import atexit
from enum import Enum
from functools import partial
import io

import logging
import os
from posix import listdir
import tarfile
import sys
from os.path import exists, join, split, splitext
import time
from pprint import pformat
from typing import List, Optional

from typing import Iterator, Optional, List, Tuple, Union, Iterator

import click
import docker  # type: ignore

from pytezos import ContractInterface, __version__, pytezos
from pytezos.cli.cache import PyTezosCLICache
from pytezos.cli.github import create_deployment, create_deployment_status
from pytezos.cli.config import (
    Source,
    SourceType,
    response_to_source_type,
    DEFAULT_LIGO_IMAGE,
    DEFAULT_SMARTPY_IMAGE,
    ext_to_source_lang,
    DEFAULT_SMARTPY_PROTOCOL,
    LigoConfig,
    PyTezosConfig,
    PyTezosLockfile,
    SmartPyConfig,
    SourceLang,
)
from pytezos.context.mixin import default_network  # type: ignore
from pytezos.logging import logger
from pytezos.michelson.types.base import generate_pydoc
from pytezos.operation.result import OperationResult
from pytezos.rpc.errors import RpcError
from pytezos.sandbox.node import SandboxedNodeTestCase
from pytezos.sandbox.parameters import EDO, FLORENCE


r = partial(click.style, fg='red')
g = partial(click.style, fg='green')
b = partial(click.style, fg='blue')


def make_bcd_link(network, address):
    return f'https://better-call.dev/{network}/{address}'


def _input(option: str, default):
    input_str = b(f'{option} [') + g(default or '') + b(']: ')
    return input(input_str) or default


# TODO: Move to pytezos.contract
def get_contract(path: str) -> ContractInterface:
    if exists(path):
        contract = ContractInterface.from_file(path)
    else:
        network, address = path.split(':')
        contract = pytezos.using(shell=network).contract(address)
    return contract


def create_directory(path: str):
    path = join(os.getcwd(), path)
    if not exists(path):
        os.mkdir(path)
    return path


def get_docker_client():
    return docker.from_env()


def run_container(
    image: str,
    command: str,
    copy_source: Optional[List[str]] = None,
    copy_destination: Optional[str] = None,
    mounts: Optional[List[docker.types.Mount]] = None,
) -> docker.models.containers.Container:

    if copy_source is None:
        copy_source = []
    if mounts is None:
        mounts = []

    client = get_docker_client()
    try:
        client.images.get(image)
    except docker.errors.ImageNotFound:
        for line in client.api.pull(image, stream=True, decode=True):
            logger.info(line)

    container = client.containers.create(
        image=image,
        command=command,
        detach=True,
        mounts=mounts,
    )

    if copy_source and copy_destination:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
            for filename in copy_source:
                _, short_filename = split(filename)
                archive.add(filename, arcname=short_filename)
        buffer.seek(0)
        container.put_archive(
            copy_destination,
            buffer,
        )

    container.start()
    return container


def wait_container(
    container: docker.models.containers.Container,
    error: str,
) -> bool:
    result = container.wait()
    status_code = int(result['StatusCode'])
    if status_code:
        for line in container.logs(stream=True):
            print(line.decode().rstrip())
        print(r(error))
        return False
    return True


@click.group()
@click.version_option(__version__)
@click.pass_context
def cli(ctx, *_args, **_kwargs):
    logging.basicConfig()
    cache = PyTezosCLICache()
    atexit.register(cache.sync)
    ctx.obj = dict(cache=cache)


@cli.command(help='Manage contract storage')
@click.option('--action', '-a', type=str, help='One of `schema`, `default`.')
@click.option('--path', '-p', type=str, help='Path to the .tz file, or the following uri: <network>:<KT-address>')
@click.pass_context
def storage(_ctx, action: str, path: str) -> None:
    contract = get_contract(path)
    if action == 'schema':
        logger.info(generate_pydoc(type(contract.storage.data), title='storage'))
    elif action == 'default':
        logger.info(pformat(contract.storage.dummy()))
    else:
        raise Exception('Action must be either `schema` or `default`')


@cli.command(help='Manage contract parameter')
@click.option('--action', '-a', type=str, default='schema', help='One of `schema`')
@click.option('--path', '-p', type=str, help='Path to the .tz file, or the following uri: <network>:<KT-address>')
@click.pass_context
def parameter(_ctx, action: str, path: str) -> None:
    contract = get_contract(path)
    if action == 'schema':
        logger.info(contract.parameter.__doc__)
    else:
        raise Exception('Action must be `schema`')


@cli.command(help='Activate and reveal key from the faucet file')
@click.option('--path', '-p', type=str, help='Path to the .json file downloaded from https://faucet.tzalpha.net/')
@click.option('--network', '-n', type=str, default=default_network, help='Default is edo2net')
@click.pass_context
def activate(_ctx, path: str, network: str) -> None:
    ptz = pytezos.using(key=path, shell=network)
    logger.info(
        'Activating %s in the %s',
        ptz.key.public_key_hash(),
        network,
    )

    if ptz.balance() == 0:
        try:
            opg = ptz.reveal().autofill().sign()
            logger.info('Injecting reveal operation:')
            logger.info(pformat(opg.json_payload()))
            opg.inject(_async=False)
        except RpcError as e:
            logger.critical(pformat(e))
            sys.exit(-1)
        else:
            logger.info('Activation succeeded! Claimed balance: %s ꜩ', ptz.balance())
    else:
        logger.info('Already activated')

    try:
        opg = ptz.reveal().autofill().sign()
        logger.info('Injecting reveal operation:')
        logger.info(pformat(opg.json_payload()))
        opg.inject(_async=False)
    except RpcError as e:
        logger.critical(pformat(e))
        sys.exit(-1)
    else:
        logger.info('Your key %s is now active and revealed', ptz.key.public_key_hash())


@cli.command(help='Deploy contract to the specified network')
@click.option('--path', '-p', type=str, help='Path to the .tz file')
@click.option('--storage', type=str, default=None, help='Storage in JSON format (not Micheline)')
@click.option('--network', '-n', type=str, default=default_network, help='Default is edo2net')
@click.option('--key', type=str, default=None)
@click.option('--github-repo-slug', type=str, default=None)
@click.option('--github-oauth-token', type=str, default=None)
@click.option('--dry-run', type=bool, default=False, help='Set this flag if you just want to see what would happen')
@click.pass_context
def deploy(
    _ctx,
    path: str,
    storage: Optional[str],  # pylint: disable=redefined-outer-name
    network: str,
    key: Optional[str],
    github_repo_slug: Optional[str],
    github_oauth_token: Optional[str],
    dry_run: bool,
):
    ptz = pytezos.using(shell=network, key=key)
    logger.info('Deploying contract using %s in the %s', ptz.key.public_key_hash(), network)

    contract = get_contract(path)
    try:
        opg = ptz.origination(script=contract.script(initial_storage=storage)).autofill().sign()
        logger.info('Injecting origination operation:')
        logger.info(pformat(opg.json_payload()))

        if dry_run:
            logger.info(pformat(opg.preapply()))
            sys.exit(0)
        else:
            opg = opg.inject(_async=False)
    except RpcError as e:
        logger.critical(pformat(e))
        sys.exit(-1)
    else:
        originated_contracts = OperationResult.originated_contracts(opg)
        if len(originated_contracts) != 1:
            raise Exception('Operation group must has exactly one originated contract')
        bcd_link = make_bcd_link(network, originated_contracts[0])
        logger.info('Contract was successfully deployed: %s', bcd_link)

        if github_repo_slug:
            deployment = create_deployment(
                github_repo_slug,
                github_oauth_token,
                environment=network,
            )
            logger.info(pformat(deployment))
            status = create_deployment_status(
                github_repo_slug,
                github_oauth_token,
                deployment_id=deployment['id'],
                state='success',
                environment=network,
                environment_url=bcd_link,
            )
            logger.info(status)


@cli.command(help='Run SmartPy CLI command "test"')
@click.option('--path', '-p', type=str, help='Path to script', default='script.py')
@click.option('--output-directory', '-o', type=str, help='Output directory', default='./smartpy-output')
@click.option('--protocol', type=click.Choice(['delphi', 'edo', 'florence', 'proto10']), help='Protocol to use', default='edo')
@click.option('--image', '-i', type=str, help='Version or tag of SmartPy to use', default=DEFAULT_SMARTPY_IMAGE)
@click.pass_context
def smartpy_test(
    _ctx,
    path: str,
    output_directory: str,
    protocol: str,
    image: str,
):
    output_directory = create_directory(output_directory)
    _, filename = split(path)
    click.echo(b('Testing ') + g(filename) + b(' with SmartPy'))

    container = run_container(
        image=image,
        command=f'test /root/smartpy-cli/{filename} /root/output --protocol {protocol}',
        copy_source=[path],
        copy_destination='/root/smartpy-cli/',
        mounts=[
            docker.types.Mount(
                target='/root/output',
                source=output_directory,
                type='bind',
            )
        ],
    )
    wait_container(container, f'Failed to test {filename}')
    container.remove()


@cli.command(help='Run SmartPy CLI command "compile"')
@click.option('--path', '-p', type=str, help='Path to script', default='script.py')
@click.option('--output-directory', '-o', type=str, help='Output directory', default='./smartpy-output')
@click.option('--protocol', type=click.Choice(['delphi', 'edo', 'florence', 'proto10']), help='Protocol to use', default='edo')
@click.option('--image', '-t', type=str, help='Version or tag of SmartPy to use', default=DEFAULT_SMARTPY_IMAGE)
@click.pass_context
def smartpy_compile(
    ctx,
    path: str,
    output_directory: str,
    protocol: str,
    image: str,
):
    if not ctx.obj['cache'].compilation_needed(path):
        quit()

    output_directory = create_directory(output_directory)
    _, filename = split(path)
    click.echo(b('Compiling ') + g(filename) + b(' with SmartPy'))

    container = run_container(
        image=image,
        command=f'compile /root/smartpy-cli/{filename} /root/output --protocol {protocol}',
        copy_source=[path],
        copy_destination='/root/smartpy-cli/',
        mounts=[
            docker.types.Mount(
                target='/root/output',
                source=output_directory,
                type='bind',
            )
        ],
    )
    success = wait_container(container, f'Failed to compile {filename}')
    if not success:
        ctx.obj['cache'].compilation_failed(path)
    container.remove()


@cli.command(help='Compile project')
@click.pass_context
def compile(ctx):
    config = PyTezosConfig.load()
    lockfile = PyTezosLockfile.load()

    for path, source in lockfile.sources.items():
        print(path, source)
        if source.type != SourceType.contract:
            continue

        if source.lang == SourceLang.SmartPy:
            ctx.invoke(
                smartpy_compile,
                path=path,
                output_directory=f'build/{source.alias}',
                protocol=config.smartpy.protocol,
                image=config.smartpy.image,
            )
        elif source.lang == SourceLang.LIGO:
            ctx.invoke(
                ligo_compile_contract,
                path=path,
                workdir=os.path.join(os.getcwd(), 'src'),
                entrypoint=source.entrypoint,
                output_directory=f'build/{source.alias}',
                image=config.ligo.image,
            )

        else:
            raise NotImplementedError


# @cli.command(help='Test project')
# @click.pass_context
# def test(
#     ctx,
# ):
#     for type_, name, path in discover_contracts():
#         if type_ == SourceLang.smartpy:
#             ctx.invoke(
#                 smartpy_test,
#                 path=path,
#                 output_directory=f'build/{name}',
#                 protocol='florence',
#             )


@cli.command(help='Init project')
@click.pass_context
def init(
    ctx,
):
    name = _input('Project name', os.path.split(os.getcwd())[1])
    description = _input('Description', None)
    license = _input('License', None)
    config = PyTezosConfig(
        name=name,
        description=description,
        license=license,
    )

    if click.confirm(b('Configure SmartPy compiler?')):
        smartpy_image = _input('SmartPy Docker image', DEFAULT_SMARTPY_IMAGE)
        smartpy_protocol = _input('SmartPy protocol', DEFAULT_SMARTPY_PROTOCOL)
        config.smartpy = SmartPyConfig(
            image=smartpy_image,
            protocol=smartpy_protocol,
        )

    if click.confirm(b('Configure LIGO compiler?')):
        ligo_image = _input('LIGO Docker image', DEFAULT_LIGO_IMAGE)
        config.ligo = LigoConfig(
            image=ligo_image,
        )

    config.save()


@cli.command(help='Update project')
@click.pass_context
def update(
    ctx,
):
    lockfile = PyTezosLockfile.load()
    cwd = os.getcwd()
    src_path = create_directory('src')

    for root, dirs, files in os.walk(src_path):
        for file in files:
            name, ext = splitext(file)
            relpath = os.path.join(root, file).replace(cwd, '')[1:]
            source_lang = ext_to_source_lang.get(ext)
            if not source_lang:
                continue
            if relpath in lockfile.skipped or relpath in lockfile.sources:
                continue

            print(b(f'Found {source_lang.value} source ') + g(relpath))
            response = _input('(C)ontract (S)torage (P)arameter (L)ambda (M)etadata', 'skip').capitalize()
            source_type = response_to_source_type.get(response)
            if not source_type:
                lockfile.skipped.append(relpath)
                continue

            alias = _input('Source alias', name)
            if source_lang == SourceLang.LIGO:
                entrypoint = _input('Entrypoint', 'main')
            else:
                entrypoint = None
            lockfile.sources[relpath] = Source(
                type=source_type,
                lang=source_lang,
                alias=alias,
                entrypoint=entrypoint,
            )

    lockfile.save()


@cli.command(help='Run containerized sandbox node')
@click.option('--image', type=str, help='Docker image to use', default=SandboxedNodeTestCase.IMAGE)
@click.option('--protocol', type=click.Choice(['florence', 'edo']), help='Protocol to use', default='florence')
@click.option('--port', '-p', type=int, help='Port to expose', default=8732)
@click.option('--interval', '-i', type=float, help='Interval between baked blocks (in seconds)', default=1.0)
@click.option('--blocks', '-b', type=int, help='Number of blocks to bake before exit')
@click.pass_context
def sandbox(
    _ctx,
    image: str,
    protocol: str,
    port: int,
    interval: float,
    blocks: int,
):
    protocol = {
        'edo': EDO,
        'florence': FLORENCE,
    }[protocol]

    SandboxedNodeTestCase.PROTOCOL = protocol
    SandboxedNodeTestCase.IMAGE = image
    SandboxedNodeTestCase.PORT = port
    SandboxedNodeTestCase.setUpClass()

    blocks_baked = 0
    while True:
        try:
            logger.info('Baking block %s...', blocks_baked)
            block_hash = SandboxedNodeTestCase.get_client().using(key='bootstrap1').bake_block().fill().work().sign().inject()
            logger.info('Baked block: %s', block_hash)
            blocks_baked += 1

            if blocks and blocks_baked == blocks:
                break

            time.sleep(interval)
        except KeyboardInterrupt:
            break


@cli.command(help='Compile contract using Ligo compiler.')
@click.option('--image', '-i', type=str, help='Version or tag of Ligo compiler', default=DEFAULT_LIGO_IMAGE)
@click.option('--path', '-p', type=str, help='Path to contract')
@click.option('--workdir', '-w', type=str, default=None, help='Source directory root')
@click.option('--entrypoint', '-e', type=str, help='Entrypoint for the invocation')
@click.option('--output-directory', '-o', type=str, help='Output directory', default='./ligo-output')
@click.pass_context
def ligo_compile_contract(
    _ctx,
    image: str,
    path: str,
    workdir: Optional[str],
    entrypoint: str,
    output_directory: str,
):
    output_directory = create_directory(output_directory)
    _, filename = split(path)
    click.echo(b('Compiling ') + g(filename) + b(' with LIGO'))

    if workdir:
        mounts = [
            docker.types.Mount(
                target='/root/src',
                source=workdir,
                type='bind',
            )
        ]
        command = f'compile-contract {path} "{entrypoint}"'
    else:
        mounts = [
            docker.types.Mount(
                target=f'/root/{filename}',
                source=path,
                type='bind',
            )
        ]
        command = f'compile-contract {filename} "{entrypoint}"'

    container = run_container(
        image=image,
        command=command,
        copy_source=[path],
        copy_destination='/root/',
        mounts=mounts,
    )
    success = wait_container(container, f'Failed to compile {filename}')
    if success:
        with open(join(output_directory, 'contract.tz'), 'w+') as file:
            for line in container.logs(stream=True, stderr=False):
                file.write(line.decode())
    container.remove()


@cli.command(help='Define initial storage using Ligo compiler.')
@click.option('--image', '-t', type=str, help='Version or tag of Ligo compiler', default=DEFAULT_LIGO_IMAGE)
@click.option('--path', '-p', type=str, help='Path to contract')
@click.option('--entrypoint', '-e', type=str, help='Entrypoint for the storage', default='')
@click.option('--expression', '--exp', type=str, help='Expression for the storage', default='')
@click.option('--output-directory', '-o', type=str, help='Output directory', default='./ligo-output')
@click.pass_context
def ligo_compile_storage(
    _ctx,
    image: str,
    path: str,
    entrypoint: str,
    expression: str,
    output_directory: str,
):
    output_directory = create_directory(output_directory)
    _, filename = split(path)
    click.echo(b('Compiling ') + g(filename) + b(' with LIGO'))

    container = run_container(
        image=image,
        command=f'compile-storage {path} "{entrypoint}" "{expression}"',
        copy_source=[path],
        copy_destination='root',
    )
    success = wait_container(container, f'Failed to compile {filename}')
    if success:
        with open(join(output_directory, 'contract.tz'), 'w+') as file:
            for line in container.logs(stream=True):
                file.write(line.decode())
    container.remove()


@cli.command(help='Invoke a contract with a parameter using Ligo compiler.')
@click.option('--image', '-i', type=str, help='Version or tag of Ligo compiler', default=DEFAULT_LIGO_IMAGE)
@click.option('--path', '-p', type=str, help='Path to contract')
@click.option('--entry-point', '-ep', type=str, help='Entrypoint for the invocation')
@click.option('--expression', '-ex', type=str, help='Expression for the invocation')
@click.option('--output-directory', '-o', type=str, help='Output directory', default='./ligo-output')
@click.pass_context
def ligo_compile_parameter(_ctx, image: str, path: str, entrypoint: str, expression: str, output_directory: str):
    output_directory = create_directory(output_directory)
    _, filename = split(path)
    click.echo(b('Compiling ') + g(filename) + b(' with LIGO'))

    container = run_container(
        image=image,
        command=f'compile-parameter {path} "{entrypoint}" "{expression}"',
        copy_source=[path],
        copy_destination='root',
    )
    success = wait_container(container, f'Failed to compile {filename}')
    if success:
        with open(join(output_directory, 'contract.tz'), 'w+') as file:
            for line in container.logs(stream=True):
                file.write(line.decode())
    container.remove()


if __name__ == '__main__':
    cli(prog_name='pytezos')