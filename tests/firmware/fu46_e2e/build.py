#!/usr/bin/env python3
"""Build full target-preprocessed Agents source with native32 sanitizers.
Requires configured normal ipod6g build, ARM compiler, native multilib ASan.
ARM preprocessing preserves shipped feature flags/struct definitions. Host
compilation replaces the instruction set, not the production screen logic.
--source-root permits archived regression revisions; config stays target ipod6g.
"""
import argparse,json,shlex,subprocess
from pathlib import Path
R=Path(__file__).resolve().parents[3]
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--build',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
p.add_argument('--source-root',type=Path,default=R);a=p.parse_args()
b=a.build.resolve();o=a.out.resolve();r=a.source_root.resolve();o.mkdir(parents=True,exist_ok=True)
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
objs=[precompile('screen','#include '+json.dumps(str(r/'apps-ipod/screens/system/agents_screen.c'))+'\n'+(R/'tools/tests/fu46_e2e/screen_adapter.c').read_text())]
for name,path in [('detail','apps-ipod/screens/system/detail_layout.c'),('nav','apps-ipod/input/agents_shortcut.c'),('remote','apps-ipod/input/agents_remote.c'),('wrap','apps-ipod/draw/text_wrap.c'),('unicode','firmware/common/unicode.c'),('diacritic','firmware/common/diacritic.c'),('dialog','apps-ipod/widgets/dialog.c')]:
 objs.append(precompile(name,'#include '+json.dumps(str(r/path))))
# Fixture controller includes resolve production transport from source-root.
transport=(R/'tools/tests/fu46_e2e/transport.c').read_text().replace('#include "../podlink_firmware_test.c"','#include '+json.dumps(str(r/'tools/tests/podlink_firmware_test.c'))).replace('#include "../../../firmware/usbstack/podlink_service.c"','#include '+json.dumps(str(r/'firmware/usbstack/podlink_service.c')))
(o/'transport.c').write_text(transport)
for name,path in [('transport',o/'transport.c'),('wire',r/'firmware/usbstack/podlink_wire.c'),('log',r/'firmware/usbstack/remote_log.c'),('model',r/'apps-ipod/screens/system/herdremote_model.c')]:
 obj=str(o/(name+'.o'));subprocess.run(flags+['-I'+str(R/'tools/tests/podlink_stubs'),'-I'+str(r/'apps-ipod/screens/system'),'-I'+str(r/'firmware/export'),'-I'+str(r/'firmware/usbstack'),'-c',str(path),'-o',obj],check=True);objs.append(obj)
subprocess.run(flags+['-no-pie','-Wl,--gc-sections',*objs,'-o',str(o/'agents-e2e')],check=True)
print('full production screen + service native32 ASan/UBSan build PASS')
