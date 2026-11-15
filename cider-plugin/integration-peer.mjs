// Offline integration peer. The test passes the PodBox secret over stdin.
import {connectBridge} from './core.mjs';
import {makeHost} from './mock-host.mjs';
let input='';for await(const chunk of process.stdin){input+=chunk;}
const config=JSON.parse(input),{host}=makeHost();
const stop=connectBridge(host,config);
process.on('SIGTERM',()=>{stop();process.exit(0);});
