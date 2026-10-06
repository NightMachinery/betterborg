import asyncio
import shlex
import sys
from types import MappingProxyType
import unittest

from brish import bsh

from uniborg import util


class _FormattingBrish:
    zstring = staticmethod(bsh.zstring)

    def send_cmd(self, cmd, *args, **kwargs):
        return shlex.split(cmd), args, kwargs


class ZaNamespaceTests(unittest.TestCase):
    def setUp(self):
        self.brish = _FormattingBrish()

    def test_caller_frame_interpolation_and_command_arguments(self):
        async def caller():
            resolution = 100
            img = "/tmp/heat map's output.png"
            return await util.za(
                "printf %s {resolution} {img}",
                "positional",
                bsh=self.brish,
                cmd_stdin="{}",
            )

        self.assertEqual(
            asyncio.run(caller()),
            (
                ["printf", "%s", "100", "/tmp/heat map's output.png"],
                ("positional",),
                {"cmd_stdin": "{}"},
            ),
        )

    def test_explicit_frame_proxy(self):
        async def caller():
            value = "sentinel"
            namespace = sys._getframe().f_locals
            return await util.za("printf %s {value}", bsh=self.brish, locals_=namespace)

        self.assertEqual(asyncio.run(caller())[0], ["printf", "%s", "sentinel"])

    def test_explicit_mapping_supports_nested_expressions_without_mutation(self):
        namespace = {"values": [2, 3]}
        result = asyncio.run(
            util.za(
                "printf %s {sum(x for x in values)}",
                bsh=self.brish,
                locals_=MappingProxyType(namespace),
            )
        )
        self.assertEqual(result[0], ["printf", "%s", "5"])
        self.assertEqual(namespace, {"values": [2, 3]})

    def test_explicit_empty_namespace_does_not_fall_back_to_caller(self):
        async def caller():
            value = "must not leak into explicit namespace"
            return await util.za("{value}", bsh=self.brish, locals_={})

        with self.assertRaises(NameError):
            asyncio.run(caller())

    def test_getframe_selects_outer_caller(self):
        async def wrapper():
            return await util.za("{value}", bsh=self.brish, getframe=2)

        async def caller():
            value = "outer caller"
            return await wrapper()

        self.assertEqual(asyncio.run(caller())[0], ["outer caller"])
