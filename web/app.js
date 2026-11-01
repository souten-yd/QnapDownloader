"use strict";
let csrf="", selectedVersion="", rapidgatorSnapshot=null;
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
   const boot=await api("/api/setup/status");
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
  itemLine($("status"),"JDownloader 2："+(s.jd2.connected?"接続中":"未接続"),s.jd2.connected?("Core revision "+s.jd2.revision):(s.jd2.reason||"接続設定を確認してください"));
 }
 if(tab==="settings"){
   const s=await api("/api/settings");
   $("selectedFolder").textContent=s.download_dir ? "保存先："+s.download_dir : "保存先：未選択";
   await folders("");
   const cfg=await api("/api/jd2/config");
   $("jd2ConfigState").textContent=cfg.configured ?
     "MyJD設定済み / デバイス: "+cfg.device_name+
       " / JD2更新: "+(cfg.can_update?"明示許可済み":"無効")+
       " / リンク送信: "+(cfg.can_send?"明示許可済み":"無効") :
     "MyJD未設定: "+(cfg.reason||"接続設定を作成してください");
 }
 if(tab==="queue"){
   clear("queueItems");
   const q=await api("/api/links");
   for(const link of q.items){
    const container=element("div",undefined,"item");
    container.appendChild(element("span",link.url));
    const status=element("span",link.state==="staged"?"保留中（自動送信なし）":
      link.state==="sending"||link.state==="uncertain"?"送信結果未確認・JD2で確認してください":
      link.state==="sent"?"JD2へ送信済み":"状態: "+link.state);
    container.appendChild(status);
    if(link.state==="staged"){
      const send=element("button","JD2に手動で送る","secondary");
      send.addEventListener("click",()=>perform(async()=>{
        if(!confirm("このリンクをJD2のLinkGrabberへ送ります。自動開始はOFFです。既存のPackagizerルールによりフォルダが変わる可能性があります。続けますか？"))return;
        await api("/api/links/send",{id:link.id});await refresh("queue");
        flash("JD2のLinkGrabberへ登録しました。処理はまだ開始していません");
      }));
      container.appendChild(send);
    }
    $("queueItems").appendChild(container);
   }
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
 const jd=await api("/api/jd2/status");
 $("jd2Status").textContent=jd.connected ?
   "JD2接続中 / Core revision: "+(jd.revision||"不明")+" / 更新: "+(jd.update_available?"あり":"なし") :
   "JD2未接続: "+(jd.reason||"接続アダプタの構成待ち");
 $("jd2Check").disabled=!jd.connected||!jd.can_update;
 $("jd2Update").disabled=!jd.connected||!jd.can_update||!jd.update_available;
 $("jd2Preset").disabled=!jd.connected||!jd.can_preset;
}
document.addEventListener("DOMContentLoaded",()=>{
 const plainLan = location.protocol==="http:" &&
   !["localhost","127.0.0.1","::1","[::1]"].includes(location.hostname);
 visible("lanSecurityWarning",plainLan);
 $("setupButton").addEventListener("click",()=>perform(async()=>{
  await api("/api/setup",{password:$("setupPassword").value});await go();
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
 $("rapidgatorText").addEventListener("input",()=>{
  rapidgatorSnapshot=null;
  $("rapidgatorStage").disabled=true;
  $("rapidgatorResult").replaceChildren();
 });
 $("rapidgatorPreview").addEventListener("click",()=>perform(async()=>{
  const content=$("rapidgatorText").value;
  const preview=await api("/api/rapidgator/extract",{content,limit:20,commit:false});
  rapidgatorSnapshot={content,digest:preview.digest,limit:20,count:preview.count};
  $("rapidgatorStage").disabled=preview.count===0;
  const list=$("rapidgatorResult");list.replaceChildren();
  list.appendChild(element("p","Rapidgatorリンク: "+preview.count+"件（未保存・未送信）"));
  for(const url of preview.items)list.appendChild(element("div",url,"item"));
 }));
 $("rapidgatorStage").addEventListener("click",()=>perform(async()=>{
  const snapshot=rapidgatorSnapshot;
  if(!snapshot||$("rapidgatorText").value!==snapshot.content){
    flash("貼り付け内容が変化しました。再抽出してください");return;
  }
  if(!confirm(snapshot.count+"件のリンクをHubの保留キューに保存します。JD2への送信・ダウンロードは開始しません。よろしいですか？"))return;
  $("rapidgatorStage").disabled=true;
  rapidgatorSnapshot=null;
  const staged=await api("/api/rapidgator/extract",
     {content:snapshot.content,limit:snapshot.limit,digest:snapshot.digest,commit:true});
  $("rapidgatorResult").replaceChildren(element("p",
    "保留キューへ新規 "+staged.staged+"件を登録しました。JD2へは未送信です。"));
  await refresh("queue");
  flash("保存完了。必要なら保留キューから個別にJD2へ送ってください");
 }));
 $("bookmarkAdd").addEventListener("click",()=>perform(async()=>{
  await api("/api/bookmarks",{title:$("bookmarkName").value,url:$("bookmarkUrl").value.trim()});
  $("bookmarkName").value="";$("bookmarkUrl").value="";await refresh("bookmarks");
 }));
 $("jd2TestConnection").addEventListener("click",()=>perform(async()=>{
  $("jd2TestResult").textContent="JD2の認証済み接続を確認しています。操作や更新は行いません。";
  try {
    const response=await api("/api/jd2/connect/test",{});
    $("jd2TestResult").textContent="接続できました。Core revision "+response.revision+
      " / 更新: "+(response.update_available?"あり":"なし");
  } catch(err) {
    $("jd2TestResult").textContent="接続できませんでした: "+err.message;
  }
 }));
 $("folderRoots").addEventListener("click",()=>perform(()=>folders("")));
 $("updateLogButton").addEventListener("click",()=>perform(async()=>{
  const result=await api("/api/update/log");
  $("updateLog").textContent=result.log||"まだ更新ログはありません";
  visible("updateLog",true);
 }));
 $("updateCheck").addEventListener("click",()=>perform(async()=>{
  const u=await api("/api/update/check");
  selectedVersion=u.available?u.release.version:"";
  $("updateResult").textContent=u.available?"更新あり: "+u.release.version:"現在利用できる新しい正式QPKGリリースはありません";
  visible("updateApply",Boolean(u.available));
  await updateStatus();
 }));
 $("jd2Check").addEventListener("click",()=>perform(async()=>{
  const result=await api("/api/jd2/update/check",{});
  await updateStatus();
  flash(result.update_available?"JD2の更新があります":"JD2に新しい更新はありません");
 }));
 $("jd2Update").addEventListener("click",()=>perform(async()=>{
  if(!confirm("JDownloader 2本体だけを更新します。JD2のダウンロードは一時中断される場合があります。よろしいですか？"))return;
  await api("/api/jd2/update/apply",{confirmed:true});await updateStatus();
 }));
 $("jd2Preset").addEventListener("click",()=>perform(async()=>{
  const preview=await api("/api/jd2/preset/preview");
  if(!preview.applicable){
    const missing=(preview.missing||[]).join(", ");
    flash("一部のJD2設定を特定できないため適用を停止しました: "+missing);
    return;
  }
  const changed=preview.entries.filter(x=>x.change);
  if(!changed.length){flash("推奨設定からの変更はありません");return}
  if(!confirm("次のJD2設定をOFFに変更します（既存キューは変更しません）。\n"+changed.map(x=>"・"+x.label).join("\n")+"\n適用しますか？"))return;
  await api("/api/jd2/preset/apply",{confirmed:true});await updateStatus();
  flash("推奨設定を再適用し、再読込で確認しました");
 }));
 $("updateApply").addEventListener("click",()=>perform(async()=>{
  if(!selectedVersion||!confirm(selectedVersion+" にQPKGを更新します。サービスが再起動されます。続けますか？"))return;
  await api("/api/update/apply",{version:selectedVersion});await updateStatus();
 }));
 go();
});