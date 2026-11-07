const $=id=>document.getElementById(id);
let csrfToken="";
const showMessage=(target,message,success=false)=>{target.textContent=message;target.className=success?"message success":"message";};
const request=async(path,options={})=>{
  const headers={"Content-Type":"application/json",...(options.headers||{})};
  if(csrfToken) headers["X-SafeDB-CSRF"]=csrfToken;
  const response=await fetch(path,{credentials:"same-origin",cache:"no-store",...options,headers});
  let payload={};try{payload=await response.json();}catch{}
  if(response.status===401){showLogin();throw new Error(payload.detail||"需要重新登录");}
  if(!response.ok) throw new Error(payload.detail||"请求失败");
  return payload;
};
function showLogin(){ $("dashboard").hidden=true;$("login-card").hidden=false;csrfToken=""; }
function showDashboard(){ $("login-card").hidden=true;$("dashboard").hidden=false; }
function formatTime(value){if(!value)return"—";return new Date(value).toLocaleString();}
function stateLabel(state){return ({MISSING:"未录入",ACTIVE:"有效",EXPIRING:"即将过期",NEEDS_CREDENTIAL:"已过期 / 需更新",EXTERNAL:"环境变量模式"})[state]||state;}
async function refreshStatus(){
  const data=await request("/api/status");
  const list=$("datasource-list");list.replaceChildren();
  const select=$("datasource");const current=select.value;select.replaceChildren();
  for(const item of data.datasources){
    const option=document.createElement("option");option.value=item.datasource_id;option.textContent=item.datasource_id;select.append(option);
    const card=document.createElement("article");card.className="source-card";
    const top=document.createElement("div");top.className="source-top";
    const name=document.createElement("div");name.className="source-name";name.textContent=item.datasource_id;
    const badge=document.createElement("span");badge.className="state "+item.state;badge.textContent=stateLabel(item.state);
    top.append(name,badge);
    const meta=document.createElement("div");meta.className="source-meta";
    meta.textContent="凭证版本："+(item.version??"—")+"\n过期时间："+formatTime(item.expires_at);
    meta.style.whiteSpace="pre-line";
    const actions=document.createElement("div");actions.className="source-actions";
    if(item.credential_mode==="local_manual"&&item.state!=="MISSING"&&item.state!=="NEEDS_CREDENTIAL"){
      const clear=document.createElement("button");clear.className="button-secondary";clear.type="button";clear.textContent="清除凭证";
      clear.addEventListener("click",async()=>{if(!confirm("确定清除 "+item.datasource_id+" 的本地凭证？"))return;try{await request("/api/credentials/"+encodeURIComponent(item.datasource_id),{method:"DELETE"});await refreshStatus();}catch(e){alert(e.message);}});
      actions.append(clear);
    }
    card.append(top,meta,actions);list.append(card);
  }
  if(current&&[...select.options].some(o=>o.value===current))select.value=current;
  $("datasource").disabled=data.datasources.every(x=>x.credential_mode!=="local_manual");
}
function setDefaultExpiry(){
  const d=new Date(Date.now()+7*60*60*1000);d.setMinutes(d.getMinutes()-d.getTimezoneOffset());
  $("expires-at").value=d.toISOString().slice(0,16);
}
async function restoreSession(){
  try{const data=await request("/api/session");csrfToken=data.csrf_token;showDashboard();await refreshStatus();}
  catch{showLogin();}
}
$("login-form").addEventListener("submit",async event=>{
  event.preventDefault();const message=$("login-message");showMessage(message,"正在验证…",true);
  try{const data=await request("/api/login",{method:"POST",headers:{},body:JSON.stringify({password:$("login-password").value})});csrfToken=data.csrf_token;$("login-password").value="";showDashboard();await refreshStatus();}
  catch(e){showMessage(message,e.message);}
});
$("credential-form").addEventListener("submit",async event=>{
  event.preventDefault();const button=$("save-credential");const message=$("credential-message");button.disabled=true;showMessage(message,"正在验证数据库连接、只读能力和表目录，请稍候…",true);
  try{
    const databaseUrl=$("database-url").value;
    const expiresAt=new Date($("expires-at").value);
    if(!databaseUrl||Number.isNaN(expiresAt.getTime()))throw new Error("请填写连接串和有效的过期时间");
    await request("/api/credentials/"+encodeURIComponent($("datasource").value),{method:"POST",body:JSON.stringify({database_url:databaseUrl,expires_at:expiresAt.toISOString()})});
    $("database-url").value="";showMessage(message,"验证成功，凭证已更新。",true);await refreshStatus();
  }catch(e){showMessage(message,e.message);}
  finally{button.disabled=false;}
});
$("refresh").addEventListener("click",async()=>{try{await refreshStatus();}catch(e){showMessage($("credential-message"),e.message);}});
$("logout").addEventListener("click",async()=>{try{await request("/api/logout",{method:"POST",body:"{}"});}catch{}showLogin();});
setDefaultExpiry();restoreSession();
