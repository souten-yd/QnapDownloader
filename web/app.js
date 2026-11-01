"use strict";
let csrf="", selectedVersion="";
const $=id=>document.getElementById(id);
function visible(id,on){$(id).classList.toggle("hidden",!on)}
function flash(message){$("notice").textContent=message;visible("notice",true)}
async function api(path, body=null){
 const headers={};
 const opts={credentials:"same-origin",headers};
 if(body!==null){opts.method="POST";headers["Content-Type"]="application/json";headers["X-CSRF-Token"]=csrf;opts.body=JSON.stringify(body)}
 const res=await fetch(path,opts);
 const data=await res.json();
 if(!res.ok)throw new Error(data.error||"HTTP "+res.status);
 return data;
}
async function go(){
 try{
 const state=await api("/api/session");
 csrf=state.csrf||"";
 if(state.authenticated){
   visible("app",true);visible("login",false);visible("setup",false);visible("logout",true);
   await refresh("dashboard");
 }else{
   visible("app",false);visible("logout",false);
   const boot=await api("/api/bootstrap/status");
   visible("setup",!boot.configured);visible("login",boot.configured);
 }
 }catch(e){flash(e.message)}
}
async function perform(fn){
 try{await fn();visible("notice",false)}catch(e){flash(e.message)}
}
function element(tag,text,className){
 const n=document.createElement(tag); if(text!==undefined)n.textContent=String(text);
 if(className)n.className=className;
 return n;
}
function clear(id){$(id).replaceChildren()}
function itemLine(container,first,second){
 const div=element("div",first,"item");
 if(second)div.appendChild(element("span",second));
 container.appendChild(div);
}
async function refresh(tab){
 if(tab==="dashboard"){
  const s=await api("/api/status");
  $("status").replaceChildren();
  itemLine($("status"),"Web管理画面：稼働中","version "+s.version+" / 設定改訂 "+s.settings_revision);
  itemLine($("status"),"JDownloader 2：未接続",s.jd2.reason);
 }
 if(tab==="settings"){
   const s=await api("/api/settings");
   $("selectedFolder").textContent=s.download_dir ? "保存先："+s.download_dir : "保存先：未選択";
   await folders("");
 }
 if(tab==="queue"){
   clear("queueItems");
   const q=await api("/api/links");
   for(const link of q.items)itemLine($("queueItems"),link.url,"保留中（JD2未登録）");
 }
 if(tab==="bookmarks"){
   clear("bookmarkItems");
   const b=await api("/api/bookmarks");
   for(const book of b.items){
     const box=element("div",undefined,"item");
     const a=element("a",book.title);
     a.href=book.url;a.rel="noopener noreferrer";a.target="_blank";
     box.appendChild(a);box.appendChild(element("span",book.url));
     $("bookmarkItems").appendChild(box);
   }
 }
 if(tab==="updates")await updateStatus();
}
function activeTab(id){
 document.querySelectorAll(".tab").forEach(n=>n.classList.toggle("hidden",n.id!==id));
 document.querySelectorAll("nav button").forEach(n=>n.classList.toggle("active",n.dataset.tab===id));
 perform(()=>refresh(id));
}
async function folders(path){
 const data=await api("/api/folders"+(path?"?path="+encodeURIComponent(path):""));
 const picker=$("folderPicker");picker.replaceChildren();
 if(path){
   picker.appendChild(element("p","表示フォルダ: "+data.path,"path"));
   const select=element("button","このフォルダを保存する");
   select.disabled=!data.writable;
   select.addEventListener("click",()=>perform(async()=>{
     const v=await api("/api/settings/folder",{path:data.path});
     $("selectedFolder").textContent="保存先："+v.download_dir;
     flash("保存先を永続保存しました");
   }));
   picker.appendChild(select);
   const back=element("button","← 共有フォルダ一覧","secondary");
   back.addEventListener("click",()=>perform(()=>folders("")));
   picker.appendChild(back);
 }
 for(const child of data.items){
   const btn=element("button","📁 "+child.name+" "+(child.writable?"":"(読取のみ)"),"folder");
   btn.addEventListener("click",()=>perform(()=>folders(child.path)));
   picker.appendChild(btn);
 }
}
async function updateStatus(){
 const s=await api("/api/update/status");
 $("updateStatus").textContent="更新状態: "+s.phase+" — "+(s.message||s.unsupported_reason||"待機中");
 $("updateApply").disabled=Boolean(s.busy)||!s.supported;
}
document.addEventListener("DOMContentLoaded",()=>{
 $("setupButton").addEventListener("click",()=>perform(async()=>{
  await api("/api/bootstrap",{token:$("bootstrapToken").value,password:$("setupPassword").value});await go();
 }));
 $("loginButton").addEventListener("click",()=>perform(async()=>{
  const r=await api("/api/login",{password:$("password").value});
  csrf=r.csrf;$("password").value="";await go();
 }));
 $("logout").addEventListener("click",()=>perform(async()=>{await api("/api/logout",{});csrf="";await go()}));
 document.querySelectorAll("nav button").forEach(b=>b.addEventListener("click",()=>activeTab(b.dataset.tab)));
 $("queueAdd").addEventListener("click",()=>perform(async()=>{
  await api("/api/links",{url:$("queueUrl").value.trim()});$("queueUrl").value="";await refresh("queue");
 }));
 $("bookmarkAdd").addEventListener("click",()=>perform(async()=>{
  await api("/api/bookmarks",{title:$("bookmarkName").value,url:$("bookmarkUrl").value.trim()});
  $("bookmarkName").value="";$("bookmarkUrl").value="";await refresh("bookmarks");
 }));
 $("folderRoots").addEventListener("click",()=>perform(()=>folders("")));
 $("updateCheck").addEventListener("click",()=>perform(async()=>{
  const u=await api("/api/update/check");
  selectedVersion=u.available?u.release.version:"";
  $("updateResult").textContent=u.available?"更新あり: "+u.release.version:"現在の正式リリースが最新です";
  visible("updateApply",Boolean(u.available));
  await updateStatus();
 }));
 $("updateApply").addEventListener("click",()=>perform(async()=>{
  if(!selectedVersion||!confirm(selectedVersion+" にQPKGを更新します。サービスが再起動されます。続けますか？"))return;
  await api("/api/update/apply",{version:selectedVersion});await updateStatus();
 }));
 go();
});