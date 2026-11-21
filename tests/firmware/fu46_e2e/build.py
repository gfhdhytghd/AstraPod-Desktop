#!/usr/bin/env python3
"""Build full target-preprocessed Agents source with native32 sanitizers.
Requires configured normal ipod6g build, ARM compiler, native multilib ASan.
ARM preprocessing preserves shipped feature flags/struct definitions. Host
compilation replaces the instruction set, not the production screen logic.
--source-root permits archived regression revisions; config stays target ipod6g.
"""
from pathlib import Path
import argparse,json,os,shlex,subprocess,sys
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from _support import require_firmware

R = require_firmware()
sys.path.insert(0, str(R / 'tools/tests'))
from podlink_rust_build import library
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--build',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
p.add_argument('--source-root',type=Path,default=R);a=p.parse_args()
b=a.build.resolve();o=a.out.resolve();r=require_firmware(a.source_root);o.mkdir(parents=True,exist_ok=True)
os.environ.setdefault('PODLINK_TEST_BUILD', str(o))
# A previous exact compile command can be reused; -n -W changes no product source.
plan=subprocess.check_output(['make','-C',str(b),'-n','-W',str(R/'apps-ipod/screens/system/agents_screen.c'),str(b/'apps-ipod/screens/system/agents_screen.o'),'V=1'],text=True)
line=next(l for l in plan.splitlines() if ' -c ' in l and 'agents_screen.c' in l)
cmd=shlex.split(line)
if cmd[0].endswith('ccache'):cmd=cmd[1:]
cmd=cmd[:cmd.index('-c')]
cmd=[arg.replace(str(R),str(r)) if not arg.startswith('-I'+str(b)) else arg for arg in cmd]
(o/'arm-command.json').write_text(json.dumps(cmd,indent=2)+'\n')
flags=['cc','-m32','-std=gnu11','-O1','-g','-fsanitize=address,undefined','-fno-pie','-fno-omit-frame-pointer','-ffunction-sections','-fdata-sections','-Wno-attributes']
def precompile(name,src):
 source=o/(name+'.c');source.write_text(src)
 subprocess.run(cmd+['-E',str(source),'-o',str(o/(name+'.i'))],check=True)
 subprocess.run(flags+['-c',str(o/(name+'.i')),'-o',str(o/(name+'.o'))],check=True)
 return str(o/(name+'.o'))
screen = r/'apps-ipod/screens/system/agents_screen.c'
adapter = HERE/'screen_adapter.c'
objs = [precompile('screen', f'#include {json.dumps(str(screen))}\n'
                            f'#include {json.dumps(str(adapter))}\n')]
for name, path in [('color', 'apps-ipod/draw/color.c'),
                   ('theme', 'apps-ipod/ui/native_theme.c')]:
 source = r/path
 if source.is_file():
  objs.append(precompile(name, '#include '+json.dumps(str(source))))
for name,path in [('detail','apps-ipod/screens/system/detail_layout.c'),('nav','apps-ipod/input/agents_shortcut.c'),('remote','apps-ipod/input/agents_remote.c'),('wrap','apps-ipod/draw/text_wrap.c'),('unicode','firmware/common/unicode.c'),('diacritic','firmware/common/diacritic.c'),('dialog','apps-ipod/widgets/dialog.c')]:
 objs.append(precompile(name,'#include '+json.dumps(str(r/path))))
# Fixture controller includes resolve production transport from source-root.
transport=(HERE/'transport.c').read_text().replace('#include "../podlink_firmware_test.c"','#include '+json.dumps(str(r/'tools/tests/podlink_firmware_test.c')))
(o/'transport.c').write_text(transport)
for name,path in [('transport',o/'transport.c'),('log',r/'firmware/usbstack/remote_log.c'),('model',r/'apps-ipod/screens/system/herdremote_model.c')]:
 obj=str(o/(name+'.o'));subprocess.run(flags+['-I'+str(R/'tools/tests'),'-I'+str(R/'tools/tests/podlink_stubs'),'-I'+str(r/'apps-ipod/screens/system'),'-I'+str(r/'firmware/export'),'-I'+str(r/'firmware/usbstack'),'-c',str(path),'-o',obj],check=True);objs.append(obj)
objs += [str(library('i686-unknown-linux-gnu')), '-lpthread', '-ldl', '-lm']
subprocess.run(flags+['-no-pie','-Wl,--gc-sections',*objs,'-o',str(o/'agents-e2e')],check=True)
print('full production screen + Rust service native32 ASan/UBSan build PASS')
