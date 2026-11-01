// أصوات الواجهة وموسيقى الخلفية الهادئة — كلها مولّدة بالكود (Web Audio) بدون ملفات صوتية.
(function () {
    'use strict';
    const AC = window.AudioContext || window.webkitAudioContext;
    let ctx = null, master = null, reverb = null, sfxBus = null, musicBus = null;

    const get = k => { try { return localStorage.getItem(k); } catch (e) { return null; } };
    const set = (k, v) => { try { localStorage.setItem(k, v); } catch (e) {} };

    // صدى ناعم يعطي إحساس المساحة الواسعة
    function makeImpulse(seconds, decay) {
        const len = Math.floor(ctx.sampleRate * seconds);
        const buf = ctx.createBuffer(2, len, ctx.sampleRate);
        for (let c = 0; c < 2; c++) {
            const d = buf.getChannelData(c);
            for (let i = 0; i < len; i++) d[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / len, decay);
        }
        return buf;
    }

    function ensure() {
        if (!AC) return null;
        if (!ctx) {
            ctx = new AC();
            master = ctx.createDynamicsCompressor();
            master.threshold.value = -18; master.ratio.value = 3;
            master.connect(ctx.destination);
            reverb = ctx.createConvolver();
            reverb.buffer = makeImpulse(3.2, 2.6);
            const wet = ctx.createGain(); wet.gain.value = 0.55;
            reverb.connect(wet); wet.connect(master);
            sfxBus = ctx.createGain(); sfxBus.gain.value = 0.5; sfxBus.connect(master);
            musicBus = ctx.createGain(); musicBus.gain.value = 0; musicBus.connect(master);
            loadTap();
        }
        if (ctx.state === 'suspended' && !document.hidden) ctx.resume().catch(() => {});
        return ctx;
    }

    const send = (node, dry, wetAmt) => {
        const g = ctx.createGain(); g.gain.value = dry; node.connect(g); g.connect(sfxBus);
        const w = ctx.createGain(); w.gain.value = wetAmt; node.connect(w); w.connect(reverb);
    };

    function tone(freq, t, dur, { type = 'sine', vol = 0.2, to = null, attack = 0.008, wet = 0.35 } = {}) {
        const o = ctx.createOscillator(), g = ctx.createGain();
        o.type = type;
        o.frequency.setValueAtTime(freq, t);
        if (to) o.frequency.exponentialRampToValueAtTime(to, t + dur);
        g.gain.setValueAtTime(0.0001, t);
        g.gain.exponentialRampToValueAtTime(vol, t + attack);
        g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
        o.connect(g); send(g, 1, wet);
        o.start(t); o.stop(t + dur + 0.05);
    }

    let noiseBuf = null;
    function whoosh(t, dur, from, to, vol) {
        if (!noiseBuf) {
            noiseBuf = ctx.createBuffer(1, ctx.sampleRate, ctx.sampleRate);
            const d = noiseBuf.getChannelData(0);
            for (let i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1;
        }
        const n = ctx.createBufferSource(); n.buffer = noiseBuf;
        const f = ctx.createBiquadFilter(); f.type = 'bandpass'; f.Q.value = 1.4;
        f.frequency.setValueAtTime(from, t);
        f.frequency.exponentialRampToValueAtTime(to, t + dur);
        const g = ctx.createGain();
        g.gain.setValueAtTime(0.0001, t);
        g.gain.exponentialRampToValueAtTime(vol, t + dur * 0.35);
        g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
        n.connect(f); f.connect(g); send(g, 1, 0.4);
        n.start(t); n.stop(t + dur + 0.05);
    }

    // ---------- أصوات الواجهة ----------
    // النغمة الأساسية: ملف tap.wav (محرَّر: بدون طنين، بداية حادة ونهاية ناعمة)
    let tapBuf = null, tapLoading = false;
    const TAP = {
        in:    { rate: 1.0,  vol: 1.0 },  // ضغطة عادية / دخول
        open:  { rate: 1.0,  vol: 1.0 },  // فتح نافذة
        tab:   { rate: 1.05, vol: 0.9 },  // تبديل تبويب: أعلى قليلاً
        out:   { rate: 0.93, vol: 1.0 },  // رجوع / خروج: أعمق قليلاً
        close: { rate: 0.96, vol: 0.95 }, // إغلاق نافذة
    };
    // نحمّل الملف مبكراً، ونفك ترميزه عند أول لمسة
    const tapBytes = fetch('/tap.wav?v=2').then(r => r.ok ? r.arrayBuffer() : Promise.reject()).catch(() => null);
    function loadTap() {
        if (tapBuf || tapLoading) return;
        tapLoading = true;
        tapBytes.then(ab => ab ? new Promise((ok, no) => ctx.decodeAudioData(ab.slice(0), ok, no)) : Promise.reject())
            .then(b => { tapBuf = b; })
            .catch(() => { tapLoading = false; });
    }

    let lastSfx = 0;
    function sfx(kind) {
        if (get('sfxOff') === '1' || !ensure()) return;
        const now = performance.now();
        if (now - lastSfx < 70) return; // منع تكرار الصوت عند الضغط المتتالي
        lastSfx = now;
        const t = ctx.currentTime + 0.01;
        if (tapBuf) { // النغمة المحررة: نفس الصوت بطبقات مختلفة حسب نوع الضغطة
            const v = TAP[kind] || TAP.in;
            const s = ctx.createBufferSource(); s.buffer = tapBuf;
            s.playbackRate.value = v.rate * (1 + (Math.random() - 0.5) * 0.03); // تنويع خفيف حتى لا يملّ
            const g = ctx.createGain(); g.gain.value = v.vol * 1.6;
            s.connect(g); send(g, 1, 0.04);
            s.start(t);
            return;
        }
        if (kind === 'in') {          // دخول: نغمتان صاعدتان ناعمتان
            whoosh(t, 0.22, 900, 3200, 0.05);
            tone(784, t, 0.32, { vol: 0.12 });
            tone(1175, t + 0.07, 0.45, { vol: 0.09 });
        } else if (kind === 'out') {  // خروج: هبّة هواء نازلة
            whoosh(t, 0.38, 3000, 500, 0.09);
            tone(880, t, 0.35, { vol: 0.07, to: 520 });
        } else if (kind === 'open') { // فتح نافذة
            tone(659, t, 0.25, { vol: 0.09, type: 'triangle' });
            tone(988, t + 0.05, 0.35, { vol: 0.07 });
        } else if (kind === 'close') {
            tone(988, t, 0.22, { vol: 0.07, to: 659 });
            whoosh(t, 0.2, 2200, 700, 0.04);
        } else {                       // tab: نقرة قصيرة
            tone(1320, t, 0.09, { vol: 0.06, wet: 0.15 });
        }
    }

    // ---------- موسيقى الخلفية (Ambient) ----------
    // تتابع كوردات حالم يتكرر ببطء، مع نغمات متناثرة فوقه
    const midi = m => 440 * Math.pow(2, (m - 69) / 12);
    const CHORDS = [
        [50, 57, 62, 64, 69],   // Dsus2/add9
        [46, 53, 58, 62, 65],   // Bbmaj
        [43, 50, 55, 59, 62],   // G
        [45, 52, 57, 61, 64],   // A
    ];
    const SCALE = [62, 64, 66, 69, 71, 74, 76, 78, 81]; // نغمات بنتاتونيك فوق الكوردات
    const CHORD_LEN = 9;
    let playing = false, timer = null, nextChord = 0, chordIdx = 0, nextNote = 0, delay = null;

    function pad(notes, t, len) {
        const f = ctx.createBiquadFilter(); f.type = 'lowpass'; f.Q.value = 0.6;
        f.frequency.setValueAtTime(500, t);
        f.frequency.linearRampToValueAtTime(1500, t + len * 0.5);
        f.frequency.linearRampToValueAtTime(600, t + len + 3);
        const g = ctx.createGain();
        g.gain.setValueAtTime(0.0001, t);
        g.gain.linearRampToValueAtTime(0.05, t + 3);
        g.gain.setValueAtTime(0.05, t + len);
        g.gain.linearRampToValueAtTime(0.0001, t + len + 4);
        f.connect(g); g.connect(musicBus);
        const w = ctx.createGain(); w.gain.value = 0.8; g.connect(w); w.connect(reverb);
        notes.forEach((n, i) => [-6, 6].forEach(det => {
            const o = ctx.createOscillator();
            o.type = i === 0 ? 'sine' : 'sawtooth';
            o.frequency.value = midi(i === 0 ? n - 12 : n);
            o.detune.value = det;
            const og = ctx.createGain(); og.gain.value = i === 0 ? 1.4 : 0.22;
            o.connect(og); og.connect(f);
            o.start(t); o.stop(t + len + 4.2);
        }));
    }

    function pluck(n, t) {
        const o = ctx.createOscillator(), g = ctx.createGain();
        o.type = 'sine'; o.frequency.value = midi(n);
        g.gain.setValueAtTime(0.0001, t);
        g.gain.exponentialRampToValueAtTime(0.035, t + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, t + 2.2);
        o.connect(g); g.connect(musicBus); g.connect(delay);
        o.start(t); o.stop(t + 2.3);
    }

    function tick() {
        const now = ctx.currentTime;
        while (nextChord < now + 1) {
            pad(CHORDS[chordIdx % CHORDS.length], nextChord, CHORD_LEN);
            chordIdx++; nextChord += CHORD_LEN;
        }
        while (nextNote < now + 1) {
            if (Math.random() < 0.55) pluck(SCALE[Math.floor(Math.random() * SCALE.length)], nextNote);
            nextNote += [0.75, 1.5, 1.5, 2.25][Math.floor(Math.random() * 4)];
        }
    }

    function startMusic() {
        if (playing || !ensure()) return;
        playing = true;
        if (!delay) { // صدى متكرر للنغمات المتناثرة
            delay = ctx.createDelay(2); delay.delayTime.value = 0.75;
            const fb = ctx.createGain(); fb.gain.value = 0.42;
            const lp = ctx.createBiquadFilter(); lp.type = 'lowpass'; lp.frequency.value = 2200;
            delay.connect(lp); lp.connect(fb); fb.connect(delay);
            const out = ctx.createGain(); out.gain.value = 0.6; lp.connect(out); out.connect(musicBus); out.connect(reverb);
        }
        const t = ctx.currentTime;
        nextChord = t + 0.1; nextNote = t + 2.5;
        musicBus.gain.cancelScheduledValues(t);
        musicBus.gain.setValueAtTime(musicBus.gain.value, t);
        musicBus.gain.linearRampToValueAtTime(1, t + 4);
        tick();
        timer = setInterval(tick, 400);
    }

    function stopMusic() {
        if (!playing) return;
        playing = false;
        clearInterval(timer); timer = null;
        const t = ctx.currentTime;
        musicBus.gain.cancelScheduledValues(t);
        musicBus.gain.setValueAtTime(musicBus.gain.value, t);
        musicBus.gain.linearRampToValueAtTime(0, t + 1.5);
    }

    const musicOn = () => get('musicOn') === '1';

    const sfxOn = () => get('sfxOff') !== '1';

    // زر الأصوات وقائمته: مفتاح لصوت الضغط ومفتاح للموسيقى
    function updateMusicBtn() {
        const b = document.getElementById('music-btn');
        const m = musicOn(), on = sfxOn();
        if (b) {
            b.classList.toggle('on', m);
            b.querySelector('.nav-ico').textContent = m ? '🎶' : on ? '🔊' : '🔇';
        }
        const sm = document.getElementById('sw-music'), ss = document.getElementById('sw-sfx');
        if (sm) sm.checked = m;
        if (ss) ss.checked = on;
    }

    function setMusic(on) {
        set('musicOn', on ? '1' : '0');
        if (on) startMusic(); else stopMusic();
        updateMusicBtn();
    }

    function toggleMusic() { setMusic(!musicOn()); }

    function toggleSfx(on) {
        set('sfxOff', on ? '0' : '1');
        updateMusicBtn();
        if (on) { ensure(); setTimeout(() => sfx('in'), 40); } // نسمعه النغمة مباشرة
    }

    function placeMenu() {
        const m = document.getElementById('sound-menu'), b = document.getElementById('music-btn');
        if (!m || !b) return;
        const r = b.getBoundingClientRect(), w = m.offsetWidth;
        const left = Math.min(Math.max(12, r.left + r.width / 2 - w / 2), innerWidth - w - 12);
        m.style.left = left + 'px';
        if (r.top > innerHeight / 2) { // الشريط في الأسفل (الجوال): القائمة تظهر فوقه
            m.style.top = 'auto'; m.style.bottom = (innerHeight - r.top + 10) + 'px';
        } else {
            m.style.bottom = 'auto'; m.style.top = (r.bottom + 10) + 'px';
        }
    }

    function closeSoundMenu() { document.getElementById('sound-menu')?.classList.remove('show'); }

    function toggleSoundMenu() {
        const m = document.getElementById('sound-menu');
        if (!m) return;
        if (m.classList.contains('show')) { closeSoundMenu(); return; }
        updateMusicBtn();
        placeMenu();
        m.classList.add('show');
    }

    // المتصفح يمنع الصوت قبل أول ضغطة، فنشغّل الموسيقى المحفوظة عند أول تفاعل
    function armAutostart() {
        const go = () => {
            ['pointerdown', 'keydown'].forEach(e => removeEventListener(e, go, true));
            if (musicOn()) startMusic();
        };
        ['pointerdown', 'keydown'].forEach(e => addEventListener(e, go, true));
    }

    // إيقاف كل الأصوات عند الخروج من التطبيق لتوفير البطارية
    document.addEventListener('visibilitychange', () => {
        if (!ctx) return;
        if (document.hidden) ctx.suspend().catch(() => {});
        else ctx.resume().catch(() => {});
    });

    // ---------- ربط الأصوات بالتنقل والنوافذ ----------
    // نوع الصوت حسب الشيء المضغوط
    const CLICKABLE = 'button, a[href], summary, .subject-card, .media-item, .gallery img, [onclick], [role="button"]';
    function kindFor(el) {
        if (el.closest('.close-modal, .nav-logout, #lightbox')) return 'close';
        if (el.closest('.nav-btn, .sub-tab-btn, .install-tabs button, .chip-pick')) return 'tab';
        if (el.closest('.modal-overlay') && el.matches('.modal-overlay')) return 'close';
        return 'in';
    }

    function hookUI() {
        // تجهيز الصوت من لحظة اللمس حتى يكون جاهزاً عند الضغطة نفسها
        addEventListener('pointerdown', () => ensure(), { capture: true, passive: true });
        // أي ضغطة على زر أو رابط أو بطاقة تشغّل النغمة
        document.addEventListener('click', e => {
            const el = e.target.closest && e.target.closest(CLICKABLE);
            if (!el || el.disabled || el.matches('input, textarea, select')) return;
            sfx(kindFor(el));
        }, true);
        // زر الرجوع في الهاتف أو المتصفح
        addEventListener('popstate', () => { if (get('batchUser')) sfx('out'); });
        // إغلاق قائمة الأصوات عند الضغط خارجها
        document.addEventListener('click', e => {
            const m = document.getElementById('sound-menu');
            if (m && m.classList.contains('show') && !m.contains(e.target) && !e.target.closest('#music-btn')) closeSoundMenu();
        });
        addEventListener('resize', () => { if (document.querySelector('#sound-menu.show')) placeMenu(); });
        addEventListener('keydown', e => { if (e.key === 'Escape') closeSoundMenu(); });
        // زر Esc يغلق النوافذ المفتوحة
        addEventListener('keydown', e => {
            if (e.key === 'Escape' && (document.querySelector('.modal-overlay[style*="flex"]') || document.querySelector('#lightbox.show'))) sfx('close');
        }, true);
        updateMusicBtn();
        armAutostart();
    }

    window.sfx = sfx;
    window.toggleMusic = toggleMusic;
    window.setMusic = setMusic;
    window.toggleSfx = toggleSfx;
    window.toggleSoundMenu = toggleSoundMenu;
    window.hookSounds = hookUI;
})();
