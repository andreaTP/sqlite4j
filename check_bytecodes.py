#!/usr/bin/env python3
"""
Parse a Chicory-generated class file and report bytecode sizes for all func_* methods.

Usage:
  python3 check_bytecodes.py [class_file] [--threshold N] [--json]

Default class file: target/classes/io/roastedroot/sqlite4j/SQLiteModuleMachineFuncGroup_0.class
"""

import struct
import sys
import argparse
import json as json_mod
import re


def parse_class_methods(classfile_path):
    """Parse a Java class file and return {method_name: bytecode_size} for all methods."""
    with open(classfile_path, 'rb') as f:
        data = f.read()

    # Parse constant pool
    pos = 10
    cp_count = struct.unpack('>H', data[8:10])[0]
    cp = {0: None}
    i = 1
    while i < cp_count:
        tag = data[pos]
        if tag == 1:  # UTF8
            length = struct.unpack('>H', data[pos+1:pos+3])[0]
            cp[i] = data[pos+3:pos+3+length].decode('utf-8', errors='replace')
            pos += 3 + length
        elif tag in (3, 4):   # Integer, Float
            cp[i] = None; pos += 5
        elif tag in (5, 6):   # Long, Double
            cp[i] = None; pos += 9; i += 1; cp[i] = None
        elif tag in (7, 8, 16, 19, 20):  # Class, String, MethodType, Module, Package
            cp[i] = None; pos += 3
        elif tag in (9, 10, 11, 12, 17, 18):  # Field/Method/IfMethod ref, NameAndType, Dynamic, InvokeDynamic
            cp[i] = None; pos += 5
        elif tag == 15:  # MethodHandle
            cp[i] = None; pos += 4
        else:
            break
        i += 1

    # Skip access flags, this class, super class
    pos += 6

    # Skip interfaces
    iface_count = struct.unpack('>H', data[pos:pos+2])[0]
    pos += 2 + iface_count * 2

    # Skip fields
    field_count = struct.unpack('>H', data[pos:pos+2])[0]
    pos += 2
    for _ in range(field_count):
        pos += 6
        attr_count = struct.unpack('>H', data[pos:pos+2])[0]
        pos += 2
        for _ in range(attr_count):
            pos += 2
            length = struct.unpack('>I', data[pos:pos+4])[0]
            pos += 4 + length

    # Parse methods
    method_count = struct.unpack('>H', data[pos:pos+2])[0]
    pos += 2

    methods = {}
    for _ in range(method_count):
        m_name_idx = struct.unpack('>H', data[pos+2:pos+4])[0]
        pos += 6
        m_name = cp.get(m_name_idx)

        attr_count = struct.unpack('>H', data[pos:pos+2])[0]
        pos += 2
        code_length = 0
        for _ in range(attr_count):
            attr_name_idx = struct.unpack('>H', data[pos:pos+2])[0]
            attr_length = struct.unpack('>I', data[pos+2:pos+6])[0]
            attr_name = cp.get(attr_name_idx)
            if attr_name == 'Code':
                code_length = struct.unpack('>I', data[pos+6+4:pos+6+8])[0]
            pos += 6 + attr_length

        if m_name and code_length > 0:
            methods[m_name] = code_length

    return methods


def main():
    parser = argparse.ArgumentParser(description='Check bytecode sizes of func_* methods')
    parser.add_argument('classfile', nargs='?',
                        default='target/classes/io/roastedroot/sqlite4j/SQLiteModuleMachineFuncGroup_0.class',
                        help='Path to the class file')
    parser.add_argument('--threshold', type=int, default=0,
                        help='Only show methods exceeding this bytecode size (default: show all)')
    parser.add_argument('--over8k', action='store_true',
                        help='Shorthand for --threshold 8000')
    parser.add_argument('--json', action='store_true',
                        help='Output as JSON')
    parser.add_argument('--all', action='store_true',
                        help='Show all methods, not just func_*')
    args = parser.parse_args()

    threshold = 8000 if args.over8k else args.threshold

    methods = parse_class_methods(args.classfile)

    # Filter to func_* unless --all
    if not args.all:
        methods = {k: v for k, v in methods.items() if k.startswith('func_')}

    # Apply threshold
    if threshold > 0:
        methods = {k: v for k, v in methods.items() if v > threshold}

    # Sort by size descending
    sorted_methods = sorted(methods.items(), key=lambda x: -x[1])

    if args.json:
        print(json_mod.dumps({k: v for k, v in sorted_methods}, indent=2))
    else:
        total = len(sorted_methods)
        over_8k = sum(1 for _, v in sorted_methods if v > 8000)
        print(f"{'Method':<20} {'Bytecode':>10}  {'Status'}")
        print('-' * 45)
        for name, size in sorted_methods:
            status = 'OVER 8KB' if size > 8000 else ''
            print(f"  {name:<18} {size:>8,} B  {status}")
        print(f"\nTotal: {total} methods")
        if threshold == 0:
            print(f"Over 8KB: {over_8k}")


if __name__ == '__main__':
    main()
