"""Byte packing helpers for the patchkit ColdFire unit tests."""


def be32(x):
    return (x & 0xFFFFFFFF).to_bytes(4, "big")
