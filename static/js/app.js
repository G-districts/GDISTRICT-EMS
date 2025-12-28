
(function(){
  const banner = document.getElementById('banner');
  const txt = document.getElementById('banner-text');
  function showBanner(message){
    if(!banner) return;
    txt.textContent = message;
    banner.classList.remove('hidden');
    setTimeout(()=>banner.classList.add('hidden'), 12000);
  }

  const panic = document.getElementById('panicBtn');
  if(panic){
    panic.addEventListener('click', async ()=>{
      try{
        const r = await fetch('/api/panic', {method:'POST'});
        if(r.ok) showBanner('PANIC SENT');
      }catch(e){ console.error(e); }
    });
  }

  const save = document.getElementById('saveAttendance');
  if(save){
    save.addEventListener('click', async ()=>{
      const items = Array.from(document.querySelectorAll('.student'));
      const updates = [];
      items.forEach(div => {
        const name = div.dataset.name;
        const chosen = div.querySelector('.chip.selected');
        if(chosen){
          updates.push({student: name, status: chosen.dataset.status});
        }
      });
      try{
        const r = await fetch('/api/attendance', {
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify({updates})
        });
        if(r.ok) showBanner('Attendance saved');
      }catch(e){ console.error(e); }
    });

    document.querySelectorAll('.chip').forEach(ch=>{
      ch.addEventListener('click', ()=>{
        const parent = ch.closest('.student');
        parent.querySelectorAll('.chip').forEach(c=>c.classList.remove('selected'));
        ch.classList.add('selected');
      });
    });
  }

  if(window.io){
    const s = io();
    s.on('hello', ()=>{});
    s.on('alert', (data)=>{
      showBanner(`${data.mode || 'LIVE'} ${data.action}`);
      try{ new Audio('/static/notify.mp3').play(); }catch(e){}
    });
  }
})();
