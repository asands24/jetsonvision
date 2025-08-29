<!doctype html>
<html>
<head>
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Jetson Vision — Game Mode</title>
  <style>
    body { margin:0; background:#0b0f14; color:#e6f1ff; font-family:system-ui; }
    .wrap { display:grid; grid-template-columns:1fr 340px; gap:16px; padding:16px; }
    .panel { background:#121923; border-radius:16px; padding:12px; }
    #stage { position:relative; width:100%; max-width:100%; }
    #video { width:100%; border-radius:16px; display:block; }
    #overlay { position:absolute; top:0; left:0; pointer-events:none; }
    .score { font-size:28px; font-weight:700; }
    .pill { display:inline-block; padding:4px 8px; border-radius:999px; background:#1e2a36; margin:4px; }
    .row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
    .stack { display:grid; gap:8px; }
    button { background:#2a7fff; border:0; color:white; padding:10px 14px; border-radius:10px; cursor:pointer; }
    button.ghost { background:transparent; border:1px solid #2a7fff; }
    .muted { opacity:.7; }
    .big { font-size:22px; font-weight:700; }
    .timer { font-variant-numeric: tabular-nums; font-size:32px; font-weight:800; }
    .banner { text-align:center; padding:8px; border-radius:10px; background:#0d1420; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="panel" id="stage">
      <img id="video" src="/video" />
      <canvas id="overlay"></canvas>
    </div>

    <div class="panel stack">
      <div class="row" style="justify-content:space-between">
        <div class="timer" id="timer">60</div>
        <div class="score">Score: <span id="scoreVal">0</span></div>
      </div>
      <div class="row">
        <div class="big">High Score: <span id="highScore">0</span></div>
        <button class="ghost" onclick="resetHigh()">Reset High</button>
      </div>

      <div class="banner muted" id="status">Tap Start to play a 60s round. Hit targets for points. Different targets within 2s = COMBO!</div>

      <div class="row">
        <button id="startBtn" onclick="startGame()">▶️ Start</button>
        <button id="stopBtn" class="ghost" onclick="stopGame()" disabled>⏹ Stop</button>
        <button onclick="toggleAudio()">🔊 Toggle Audio</button>
        <button onclick="resetScore()">♻️ Reset Score</button>
      </div>

      <div class="stack">
        <div>Targets (with multipliers):</div>
        <div id="targets"></div>
      </div>

      <p class="muted">Tip: tap the page once to enable sound & vibration on mobile. Works over Tailscale at <span id="hostSpan" class="muted"></span></p>
    </div>
  </div>

  <script src="https://cdn.socket.io/4.7.5/socket.io.min.js" crossorigin="anonymous"></script>
  <script>
    // --- Configurable game settings ---
    const ROUND_SECS = 60;
    const RARITY = {
      "person": 1,
      "bottle": 1,
      "cup": 1,
      "cell phone": 2,
      "book": 2,
      "sports ball": 5
    };
    const TARGETS = Object.keys(RARITY);
    const SAME_CLASS_COOLDOWN_MS = 1000;  // don't score same class more than once per second
    const COMBO_WINDOW_MS = 2000;         // different classes within 2s => +3 combo bonus
    const MIN_CONF = 0.50;                // only count confident detections

    // --- DOM ---
    const socket = io();
    const video = document.getElementById('video');
    const canvas = document.getElementById('overlay');
    const ctx = canvas.getContext('2d');
    const scoreEl = document.getElementById('scoreVal');
    const timerEl = document.getElementById('timer');
    const highEl = document.getElementById('highScore');
    const statusEl = document.getElementById('status');
    const hostSpan = document.getElementById('hostSpan');

    // --- State ---
    let gameActive = false;
    let timeLeft = ROUND_SECS;
    let tickHandle = null;
    let score = 0;
    let audioOn = true;

    // Anti-spam per class timestamps
    const lastHitMs = {};
    // Recent different-class hits for combo
    let lastClass = null;
    let lastClassMs = 0;

    // UI init
    hostSpan.textContent = window.location.host;
    highEl.textContent = Number(localStorage.getItem("jetson_high_score") || 0);
    document.getElementById('targets').innerHTML =
      TARGETS.map(t=>`<span class="pill">${t} ×${RARITY[t]}</span>`).join('');

    function fitCanvas(){
      canvas.width = video.clientWidth;
      canvas.height = video.clientHeight;
    }
    window.addEventListener('resize', fitCanvas);
    video.addEventListener('load', fitCanvas);

    // --- Sounds & Haptics ---
    function playBeep(freq=880, dur=0.07, vol=0.1){
      if(!audioOn) return;
      const a = new AudioContext();
      const o = a.createOscillator();
      const g = a.createGain();
      o.connect(g); g.connect(a.destination);
      o.type="sine"; o.frequency.setValueAtTime(freq, a.currentTime);
      g.gain.setValueAtTime(vol, a.currentTime);
      o.start(); o.stop(a.currentTime+dur);
    }
    function speak(text){
      if(!audioOn) return;
      try { speechSynthesis.cancel(); speechSynthesis.speak(new SpeechSynthesisUtterance(text)); } catch(e){}
    }
    function vibrate(pattern=[60,30,60]){
      if (navigator.vibrate) navigator.vibrate(pattern);
    }
    function comboFlash(){
      playBeep(1200, 0.12, 0.15);
      vibrate([100,50,100,50,150]);
      statusEl.textContent = "🔥 COMBO! +3 bonus";
      setTimeout(()=>statusEl.textContent = "Nice streak…", 800);
    }

    // --- Game Loop ---
    function startGame(){
      if (gameActive) return;
      gameActive = true;
      score = 0; scoreEl.textContent = score;
      timeLeft = ROUND_SECS; timerEl.textContent = timeLeft;
      lastClass = null; lastClassMs = 0;
      Object.keys(lastHitMs).forEach(k=>delete lastHitMs[k]);
      statusEl.textContent = "Go! Hit different targets for combos!";
      document.getElementById('startBtn').disabled = true;
      document.getElementById('stopBtn').disabled = false;

      tickHandle = setInterval(()=>{
        if (!gameActive) return;
        timeLeft -= 1; timerEl.textContent = timeLeft;
        if (timeLeft <= 0) endGame();
      }, 1000);
      playBeep(660, 0.15, 0.12); vibrate([120,40,120]);
    }

    function stopGame(){
      if (!gameActive) return;
      endGame(true);
    }

    function endGame(manual=false){
      gameActive = false;
      clearInterval(tickHandle); tickHandle = null;
      document.getElementById('startBtn').disabled = false;
      document.getElementById('stopBtn').disabled = true;

      // High score
      const high = Number(localStorage.getItem("jetson_high_score") || 0);
      if (score > high){
        localStorage.setItem("jetson_high_score", String(score));
        highEl.textContent = score;
        statusEl.textContent = manual ? "Stopped. New High Score! 🏆" : "Time! New High Score! 🏆";
        speak("New high score!");
        playBeep(1000, 0.25, 0.18);
      } else {
        statusEl.textContent = manual ? "Stopped. Play again!" : "Time! Great run!";
        playBeep(500, 0.12, 0.1);
      }
      vibrate([160,60,160]);
    }

    function resetScore(){ score=0; scoreEl.textContent=score; statusEl.textContent="Score reset."; }
    function resetHigh(){ localStorage.removeItem("jetson_high_score"); highEl.textContent=0; statusEl.textContent="High score cleared."; }
    function toggleAudio(){ audioOn = !audioOn; statusEl.textContent = audioOn ? "Audio ON" : "Audio OFF"; }

    // --- Drawing helpers (AR-ish)
    function drawHat(x,y,w,h){
      ctx.font = Math.floor(h*0.6)+"px serif";
      ctx.fillText("🎩", x, y - 4);
      ctx.font = "14px monospace";
    }
    function drawSparkles(x,y,w,h){
      ctx.font = "20px serif";
      ctx.fillText("✨", x+w-22, y+22);
    }

    // --- Socket events from server
    socket.on('dets', payload=>{
      const {W,H,items} = payload;
      fitCanvas();
      ctx.clearRect(0,0,canvas.width,canvas.height);

      const sx = canvas.width / W;
      const sy = canvas.height / H;

      // render boxes + labels
      items.forEach(d=>{
        const [x1,y1,x2,y2] = d.xyxy;
        const x = x1*sx, y = y1*sy, w = (x2-x1)*sx, h = (y2-y1)*sy;
        ctx.strokeStyle="#2a7fff"; ctx.lineWidth=2; ctx.strokeRect(x,y,w,h);
        ctx.fillStyle="#2a7fff"; ctx.fillText(`${d.name} ${Math.round(d.conf*100)}%`, x+4, y-6);
        drawHat(x,y,w,h); drawSparkles(x,y,w,h);
      });

      // scoring logic only while game is active
      if (!gameActive) return;

      const now = performance.now();
      // score unique, confident target classes present in this frame
      const hitClasses = new Set();
      for (const d of items){
        const cls = d.name;
        if (!TARGETS.includes(cls)) continue;
        if (d.conf < MIN_CONF) continue;

        // anti-spam: same class cooldown
        const last = lastHitMs[cls] || 0;
        if ((now - last) < SAME_CLASS_COOLDOWN_MS) continue;
        lastHitMs[cls] = now;
        hitClasses.add(cls);
      }

      // apply scoring
      hitClasses.forEach(cls=>{
        const base = 1 * (RARITY[cls] || 1);
        let add = base;

        // combo if different class within window
        if (lastClass && lastClass !== cls && (now - lastClassMs) <= COMBO_WINDOW_MS){
          add += 3;  // combo bonus
          comboFlash();
        } else {
          // small pop
          playBeep(880, 0.06, 0.1);
          vibrate([60,30,60]);
        }

        score += add;
        scoreEl.textContent = score;
        lastClass = cls;
        lastClassMs = now;

        if (cls === "sports ball") speak("Ball!");
        else if (cls === "person") speak("Human detected!");
      });

    });
  </script>
</body>
</html>

