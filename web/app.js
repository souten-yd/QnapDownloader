"use strict";
let csrf="", selectedVersion="", selectedChannel="rc", rapidgatorSnapshot=null;
let browserHistory=[], browserIndex=-1, browserCurrent="";
let downloadsPage=0, downloadsBusy=false;
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
   browserHistoryButtons();
 }else{
   visible("app",false);visible("logout",false);
   const boot=await api("/api/setup/status");
   visible("setup",!boot.configured);visible("login",boot.configured);
 }
 }catch(e){flash(e.message)}
}
async function perform(fn){
 visible("notice",false);
 try{await fn()}catch(e){flash(e.message)}
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
function readableSize(bytes){
 if(typeof bytes!=="number"||!Number.isFinite(bytes)||bytes<0)return "不明";
 if(bytes===0)return "0 B";
 const exp=Math.min(4,Math.floor(Math.log(bytes)/Math.log(1024)));
 const num=bytes/Math.pow(1024,exp);
 return (num>=10?num.toFixed(0):num.toFixed(1))+" "+["B","KiB","MiB","GiB","TiB"][exp];
}
function readableEta(seconds){
 if(typeof seconds!=="number"||!Number.isFinite(seconds)||seconds<0)return "不明";
 const minutes=Math.ceil(seconds/60);
 return minutes<1?"1分未満":minutes<60?minutes+"分":Math.floor(minutes/60)+"時間"+(minutes%60)+"分";
}
function downloadCard(item,packageItem){
 const card=element("article",undefined,"download-card");
 card.appendChild(element("strong",item.name,"download-title"));
 const parts=[];
 if(!packageItem&&item.host)parts.push("ホスト: "+item.host);
 if(packageItem&&item.child_count!==null)parts.push("ファイル: "+item.child_count+"件");
 const state=item.finished===true?"完了":item.running===true?"ダウンロード中":
   item.enabled===false?"無効":"待機/確認中";
 parts.push(state);
 if(item.status)parts.push(item.status);
 card.appendChild(element("p",parts.join(" / "),"download-detail"));
 if(item.progress!==null){
  const track=element("div",undefined,"progress-track");
  track.setAttribute("role","progressbar");
  track.setAttribute("aria-valuemin","0");
  track.setAttribute("aria-valuemax","100");
  track.setAttribute("aria-valuenow",String(item.progress));
  track.setAttribute("aria-label",item.name+" 進捗");
  const fill=element("div",undefined,"progress-fill");
  fill.style.width=item.progress+"%";
  track.appendChild(fill);card.appendChild(track);
 }
 const loaded=readableSize(item.bytes_loaded);
 const total=readableSize(item.bytes_total);
 const details="転送: "+loaded+" / "+total+
  " · 速度: "+readableSize(item.speed_bytes)+"/秒"+
  " · 残り: "+readableEta(item.eta_seconds);
 card.appendChild(element("p",details,"download-detail"));
 return card;
}
async function loadDownloads(page){
 if(downloadsBusy)return;
 downloadsBusy=true;
 $("downloadsRefresh").disabled=true;
 $("downloadsMessage").textContent="JD2から状態を取得中… ダウンロード操作は実行しません。";
 try{
  const state=await api("/api/jd2/downloads?page="+page);
  downloadsPage=state.page;
  const packages=$("downloadPackages"),links=$("downloadLinks");
  packages.replaceChildren();links.replaceChildren();
  for(const item of state.packages)packages.appendChild(downloadCard(item,true));
  for(const item of state.links)links.appendChild(downloadCard(item,false));
  if(!state.packages.length)packages.appendChild(element("p","パッケージが見つかりません。","empty-state"));
  if(!state.links.length)links.appendChild(element("p","ファイルが見つかりません。","empty-state"));
  const running=state.links.filter(x=>x.running===true).length;
  const finished=state.links.filter(x=>x.finished===true).length;
  const metrics=$("downloadsSummary");metrics.replaceChildren();
  for(const [title,value] of [["表示ファイル",state.links.length+"件"],["実行中",running+"件"],["完了",finished+"件"]]){
   const box=element("div",undefined,"metric");
   box.appendChild(element("span",title));box.appendChild(element("strong",value));metrics.appendChild(box);
  }
  $("downloadsMessage").textContent=
    "JD2から取得しました（読み取り専用）。一覧は最大30件ずつ表示します。";
  $("downloadsPage").textContent=(state.page+1)+"ページ目";
  $("downloadsPrev").disabled=state.page===0;
  $("downloadsNext").disabled=!state.has_more||state.page>=20;
 }catch(error){
  $("downloadsMessage").textContent="取得できませんでした: "+error.message+
     "。設定でMyJDownloader接続を確認してください。";
  $("downloadsPrev").disabled=true;$("downloadsNext").disabled=true;
 }finally{
  downloadsBusy=false;$("downloadsRefresh").disabled=false;
 }
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
 document.querySelectorAll("nav button").forEach(n=>{
   const current=n.dataset.tab===id;
   n.classList.toggle("active",current);
   if(current)n.setAttribute("aria-current","page");
   else n.removeAttribute("aria-current");
 });
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
function browserHistoryButtons(){
 $("browserBack").disabled=browserIndex<=0;
 $("browserForward").disabled=browserIndex<0||browserIndex>=browserHistory.length-1;
 const available=Boolean(browserCurrent);
 $("browserOpenSafari").disabled=!available;
 $("browserQueue").disabled=!available;
}
function browserDisplay(url){
 browserCurrent=url;
 const frame=$("browserFrame");
 const interactive=$("browserMode").value==="interactive";
 // Never combine allow-scripts with allow-same-origin. All remote pages
 // remain opaque-origin, cannot reach Hub DOM or invoke its same-origin API.
 frame.setAttribute("sandbox",interactive?"allow-scripts allow-forms":"");
 frame.setAttribute("referrerpolicy","no-referrer");
 frame.src=url;
 $("browserUrl").value=url;
 $("browserStatus").textContent="隔離表示中: "+url+
  " — サイトが埋め込みを禁止している場合は空白になります。Safariで開いてください。";
 browserHistoryButtons();
}
async function browserNavigate(value,record=true){
 const result=await api("/api/browser/validate?url="+encodeURIComponent(value));
 if(record){
  browserHistory=browserHistory.slice(0,browserIndex+1);
  browserHistory.push(result.url);
  browserIndex=browserHistory.length-1;
 }
 browserDisplay(result.url);
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
 $("browserOpen").addEventListener("click",()=>perform(async()=>{
  await browserNavigate($("browserUrl").value.trim());
 }));
 $("browserBack").addEventListener("click",()=>perform(async()=>{
  if(browserIndex<=0)return;
  const next=browserHistory[browserIndex-1];
  await browserNavigate(next,false);browserIndex--;browserHistoryButtons();
 }));
 $("browserForward").addEventListener("click",()=>perform(async()=>{
  if(browserIndex<0||browserIndex>=browserHistory.length-1)return;
  const next=browserHistory[browserIndex+1];
  await browserNavigate(next,false);browserIndex++;browserHistoryButtons();
 }));
 $("browserClose").addEventListener("click",()=>{
  $("browserFrame").removeAttribute("src");
  $("browserFrame").setAttribute("sandbox","");
  browserCurrent="";browserHistory=[];browserIndex=-1;
  browserHistoryButtons();
  $("browserStatus").textContent="プレビューを閉じました。";
 });
 $("browserMode").addEventListener("change",()=>{
  if(browserCurrent){
    if($("browserMode").value==="interactive" &&
       !confirm("外部サイトのスクリプトとフォームを隔離枠内で実行します。ログインやCloudflareの動作保証はありません。続けますか？")){
      $("browserMode").value="strict";return;
    }
    browserDisplay(browserCurrent);
  }
 });
 $("browserOpenSafari").addEventListener("click",()=>{
  if(!browserCurrent)return;
  window.open(browserCurrent,"_blank","noopener,noreferrer");
 });
 $("browserQueue").addEventListener("click",()=>perform(async()=>{
  if(!browserCurrent||!confirm("現在のアドレス欄のURLを保留キューに登録します。サイト内リンクは自動解析せず、JD2へも送信しません。よろしいですか？"))return;
  await api("/api/links",{url:browserCurrent});
  $("browserStatus").textContent="保留キューに登録しました。ダウンロードは開始しません。";
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
 document.querySelectorAll("[data-go]").forEach(button=>
   button.addEventListener("click",()=>activeTab(button.dataset.go)));
 $("downloadsRefresh").addEventListener("click",()=>loadDownloads(downloadsPage));
 $("downloadsPrev").addEventListener("click",()=>loadDownloads(downloadsPage-1));
 $("downloadsNext").addEventListener("click",()=>loadDownloads(downloadsPage+1));
 $("folderRoots").addEventListener("click",()=>perform(()=>folders("")));
 $("updateLogButton").addEventListener("click",()=>perform(async()=>{
  const result=await api("/api/update/log");
  $("updateLog").textContent=result.log||"まだ更新ログはありません";
  visible("updateLog",true);
 }));
 $("updateChannel").addEventListener("change",()=>{
  selectedVersion="";
  selectedChannel=$("updateChannel").value;
  $("updateResult").textContent="チャンネルを変更しました。更新を再確認してください。";
  visible("updateApply",false);
 });
 $("updateCheck").addEventListener("click",()=>perform(async()=>{
  const channel=$("updateChannel").value;
  const u=await api("/api/update/check?channel="+encodeURIComponent(channel));
  selectedVersion=u.available?u.release.version:"";
  selectedChannel=channel;
  $("updateResult").textContent=u.available?
    "更新あり: "+u.release.version+(u.release.version.includes("-rc")?"（RC版）":"（正式版）"):
    (channel==="rc"?"新しい正式版・RC版のQPKGはありません":"新しい正式版のQPKGはありません");
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
  await api("/api/update/apply",{version:selectedVersion,channel:selectedChannel});await updateStatus();
 }));
 go();
});