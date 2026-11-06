#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Offline port checks; no hardware, Pico SDK, or SDR++ tree required."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
vendor = ROOT / 'external/radio-firmware-images/lr1121/transceiver/lr1121_transceiver_0103.bin'
if not vendor.is_file():
    sys.exit('Initialize test fixture first: git submodule update --init external/radio-firmware-images')
out = ROOT / 'build' / 'nomad-tests'
out.mkdir(parents=True, exist_ok=True)
for name, impl in [('test_protocol', 'protocol'), ('test_stream_format', 'stream_format')]:
    binary = out / (name + ('.exe' if os.name == 'nt' else ''))
    subprocess.run([os.environ.get('CC', 'gcc'), '-std=c11', '-Wall', '-Wextra', '-Werror',
                    '-I' + str(ROOT / 'pico/firmware'), str(ROOT / f'pico/tests/{name}.c'),
                    str(ROOT / f'pico/firmware/{impl}.c'), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
binary=out/('test_compact.exe' if os.name=='nt' else 'test_compact')
subprocess.run([os.environ.get('CC','gcc'),'-std=c11','-Wall','-Wextra','-Werror',
                '-I'+str(ROOT/'nomad/src'),str(ROOT/'nomad/tests/test_compact.c'),
                str(ROOT/'nomad/src/compact.c'),'-lm','-o',str(binary)],check=True)
subprocess.run([str(binary)],check=True)
binary=out/('test_ook.exe' if os.name=='nt' else 'test_ook')
subprocess.run([os.environ.get('CC','gcc'),'-std=c11','-Wall','-Wextra','-Werror',
                '-I'+str(ROOT/'nomad/src'),str(ROOT/'nomad/tests/test_ook.c'),
                str(ROOT/'nomad/src/ook.c'),'-o',str(binary)],check=True)
subprocess.run([str(binary)],check=True)
for directory in ('host/tests', 'lr1121/tests'):
    subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', str(ROOT / directory), '-v'], check=True)
