G-Schools EMS — Teacher Edition + Admin
Run:
  pip install flask pyyaml requests flask-socketio simple-websocket
  python app.py   (default port 80; or set PORT=5000)

Login:
  /login  -> teacher / letmein

Admin:
  /admin (use your config.yaml admin_passcode)
  - edit Cisco phones, credentials
  - edit Asterisk page extension & AMI
  - edit branding & recipients

Alerts:
  Dashboard lets you choose DRILL or LIVE; order: Cisco -> PBX -> RSS -> Email
  Cisco uses Play:tone.raw then displays hosted XML (/xml/<action>)

ClockWise RSS:
  Point watchers to /rss/hold.xml, /rss/secure.xml, /rss/shelter.xml, /rss/evacuate.xml, /rss/lockdown.xml
