# SPDX-License-Identifier: MIT
"""PlatformIO packaging target; packages the ESP32 image only, never LR firmware."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

Import('env')


def package(source, target, env):
    root = Path(env.subst('$PROJECT_DIR')).parent
    build = Path(env.subst('$BUILD_DIR'))
    pioenv = env.subst('$PIOENV')
    board = {'radiomaster_nomad': 'radiomaster-nomad', 'radiomaster_xr4': 'radiomaster-xr4'}[pioenv]
    # Keep the Nomad path unchanged for existing docs/tools; other boards get their own folder.
    out = root / 'dist' / ('nomad' if pioenv == 'radiomaster_nomad' else board.split('-', 1)[1])
    out.mkdir(parents=True, exist_ok=True)
    platform = env.PioPlatform()
    framework = Path(platform.get_package_dir('framework-arduinoespressif32'))
    esptool = Path(platform.get_package_dir('tool-esptoolpy')) / 'esptool.py'
    inputs = [('0x1000', build / 'bootloader.bin'), ('0x8000', build / 'partitions.bin'),
              ('0xe000', framework / 'tools' / 'partitions' / 'boot_app0.bin'),
              ('0x10000', build / 'firmware.bin')]
    command = [env.subst('$PYTHONEXE'), str(esptool), '--chip', 'esp32', 'merge_bin',
               '--output', str(out / 'nomad-sdr-merged.bin'), '--flash_mode', 'dio',
               '--flash_freq', '40m', '--flash_size', '4MB']
    for offset, path in inputs:
        command.extend([offset, str(path)])
    subprocess.run(command, check=True)
    for _, path in inputs:
        shutil.copy2(path, out / path.name)
    shutil.copy2(root / 'LICENSE', out / 'LICENSE-MIT.txt')
    shutil.copy2(root / 'docs' / 'nomad.md', out / 'nomad.md')
    shutil.copy2(root / 'docs' / 'live-sdr.md', out / 'live-sdr.md')
    shutil.copy2(root / 'docs' / 'handover.md', out / 'handover.md')
    manifest = dict(board=board, mcu='esp32', hardware_validated=False,
                    merged_flash_offset='0x0', application_flash_offset='0x10000',
                    lr1121_firmware_included=False, files={})
    for name in ('nomad-sdr-merged.bin', 'firmware.bin', 'bootloader.bin', 'partitions.bin', 'boot_app0.bin'):
        data = (out / name).read_bytes()
        manifest['files'][name] = dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    print('Packaged ESP32 firmware at ' + str(out))


env.AddCustomTarget('package', '$BUILD_DIR/${PROGNAME}.bin', package,
                    title='Package Nomad firmware', description='Build merged image and SHA256 manifest')
