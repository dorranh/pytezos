from unittest import TestCase

from tests import abspath

from pytezos.repl.interpreter import Interpreter
from pytezos.michelson.converter import michelson_to_micheline
from pytezos.repl.parser import parse_expression


class OpcodeTestmap_mem_string_106(TestCase):

    def setUp(self):
        self.maxDiff = None
        self.i = Interpreter(debug=True)
        
    def test_opcode_map_mem_string_106(self):
        res = self.i.execute(f'INCLUDE "{abspath("opcodes/contracts/map_mem_string.tz")}"')
        self.assertTrue(res['success'])
        
        res = self.i.execute('RUN "foo" (Pair { Elt "foo" 0 } None)')
        self.assertTrue(res['success'])
        
        exp_val_expr = michelson_to_micheline('(Pair { Elt "foo" 0 } (Some True))')
        exp_val = parse_expression(exp_val_expr, res['result']['storage'].type_expr)
        self.assertEqual(exp_val, res['result']['storage']._val)