"""Python values to and from chia_wallet_sdk Programs.

`Clvm.alloc` encodes a list of small ints as a byte string, so nothing here
goes through it: lists, pairs and ints are built explicitly, ints with the
same minimal encoding the engine hashes with (`flpcr.clvm.int_to_atom`).
"""

from __future__ import annotations

from chia_wallet_sdk import Clvm, Program

from ..clvm import atom_of


def to_program(clvm: Clvm, value) -> Program:
    if isinstance(value, Program):
        return value
    if value is None:
        return clvm.nil()
    if isinstance(value, list):
        return clvm.list([to_program(clvm, v) for v in value])
    if isinstance(value, tuple):
        if len(value) != 2:
            raise ValueError("a pair is a tuple of two")
        return clvm.pair(to_program(clvm, value[0]), to_program(clvm, value[1]))
    return clvm.atom(atom_of(value))


def to_int(program: Program) -> int:
    atom = program.to_atom()
    if atom is None:
        raise ValueError("not an atom")
    return int.from_bytes(atom, "big", signed=True) if atom else 0


def to_bytes(program: Program) -> bytes:
    atom = program.to_atom()
    if atom is None:
        raise ValueError("not an atom")
    return atom


def to_list(program: Program) -> list:
    out = []
    while program.is_pair():
        out.append(program.first())
        program = program.rest()
    return out
