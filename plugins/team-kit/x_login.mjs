// Reauthenticate only the dedicated sns-manager profile. Never print cookie values.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawn} from 'node:child_process';
const home=path.join(os.homedir(),'.hermes/profiles/sns-manager');
const profile=path.join(home,'state/x-browser');
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
let socket;
async function main(){
  fs.mkdirSync(profile,{recursive:true,mode:0o700});
  if(fs.lstatSync(profile).isSymbolicLink())throw Error('profile_symlink');
  fs.chmodSync(profile,0o700);
  const active=path.join(profile,'DevToolsActivePort');
  // Do not attach to or close an unrelated browser, including an existing login session.
  let reuse=false;
  if(fs.existsSync(active)){
    const [port,endpoint]=fs.readFileSync(active,'utf8').trim().split('\n');
    try{
      const r=await fetch(`http://localhost:${Number(port)}/json/version`,{signal:AbortSignal.timeout(1000)});
      const m=await r.json();
      if(m.webSocketDebuggerUrl?.endsWith(endpoint)){
        console.log(JSON.stringify({status:'login_window_already_open',port:Number(port),profile}));reuse=true;
      }
    }catch{}
    if(!reuse)fs.unlinkSync(active);
  }
  // Reopen a visible tab even when the dedicated process survived its last window.
  const child=spawn('/Applications/Whale.app/Contents/MacOS/Whale',[
    '--user-data-dir='+profile,'--disable-blink-features=AutomationControlled',
    '--remote-debugging-address=localhost','--remote-debugging-port=0',
    '--no-first-run','--no-default-browser-check','--new-window','https://x.com/login'
  ],{stdio:'ignore',detached:true});
  child.unref();
  for(let n=0;n<30&&!fs.existsSync(active);n++)await sleep(1000);
  const [port,endpoint]=fs.readFileSync(active,'utf8').trim().split('\n');
  const meta=await(await fetch(`http://localhost:${Number(port)}/json/version`)).json();
  if(!meta.webSocketDebuggerUrl.endsWith(endpoint))throw Error('endpoint_mismatch');
  console.log(JSON.stringify({status:'login_window_open',port:Number(port),profile}));
  const deadline=Date.now()+30*60*1000;
  let cleared=false;
  while(Date.now()<deadline){
    const tabs=await(await fetch(`http://localhost:${Number(port)}/json/list`)).json();
    const tab=tabs.find(t=>t.type==='page'&&new URL(t.url).hostname==='x.com');
    if(tab){
      socket=new WebSocket(tab.webSocketDebuggerUrl);
      await new Promise((r,j)=>{socket.onopen=r;socket.onerror=()=>j(Error('cdp_connection'));});
      let id=0;const pending=new Map();
      socket.onmessage=e=>{const m=JSON.parse(e.data);pending.get(m.id)?.(m);pending.delete(m.id);};
      async function call(method,params={}){const n=++id;const p=new Promise((r,j)=>{const timer=setTimeout(()=>j(Error('cdp_timeout')),10000);pending.set(n,m=>{clearTimeout(timer);r(m);});});socket.send(JSON.stringify({id:n,method,params}));const m=await p;if(m.error)throw Error('cdp_error');return m.result;}
      const {cookies}=await call('Network.getAllCookies');
      if(!cleared){
        // An expired auth cookie must not be mistaken for a newly completed login.
        for(const cookie of cookies){
          if((cookie.domain==='x.com'||cookie.domain.endsWith('.x.com'))&&['auth_token','ct0'].includes(cookie.name)){
            await call('Network.deleteCookies',{name:cookie.name,domain:cookie.domain,path:cookie.path});
          }
        }
        cleared=true;
        await call('Page.navigate',{url:'https://x.com/login'});
        socket.close();socket=null;
        await sleep(15000);
        continue;
      }
      const get=name=>cookies.find(c=>(c.domain==='x.com'||c.domain.endsWith('.x.com'))&&c.name===name)?.value;
      const values=[get('auth_token'),get('ct0')];
      if(values.every(v=>v&&/^[A-Za-z0-9%._~-]+$/.test(v))){
        const file=path.join(home,'.env');const stat=fs.lstatSync(file);
        if(!stat.isFile()||stat.isSymbolicLink())throw Error('env_not_regular');
        let text=fs.readFileSync(file,'utf8');
        ['X_AUTH_TOKEN','X_CT0'].forEach((key,n)=>{
          const re=new RegExp('^'+key+'=.*$','m');
          text=re.test(text)?text.replace(re,()=>key+'='+values[n]):text.trimEnd()+'\n'+key+'='+values[n]+'\n';
        });
        const fd=fs.openSync(file,'w');try{fs.writeFileSync(fd,text);fs.fsyncSync(fd);}finally{fs.closeSync(fd);}
        fs.chmodSync(file,stat.mode&0o777);
        console.log(JSON.stringify({status:'login_saved',cookies_present:true}));
        socket.send(JSON.stringify({id:++id,method:'Browser.close'}));await sleep(500);socket.close();return;
      }
      socket.close();socket=null;
    }
    await sleep(15000);
  }
  console.log(JSON.stringify({status:'login_timeout_window_left_open',port:Number(port),profile}));
  process.exitCode=1;
}
try{await main();}catch{console.error('x-login failed; login window is left open if running');process.exitCode=1;}finally{socket?.close();}
