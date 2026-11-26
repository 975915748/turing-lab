/* 图灵实验室 · Dimension Motion
 * 仅增强 game-index.html 的现有交互；对局状态与 API 仍由原页面管理。
 */
(() => {
  'use strict';

  if (window.turingDimensionUI) return;

  document.body.dataset.theme = 'cyber';
  try { localStorage.setItem('turing_theme', 'cyber'); } catch (_) {}

  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const finePointer = window.matchMedia('(pointer: fine)');
  const scene = document.createElement('div');
  scene.className = 'dimension-scene';
  scene.setAttribute('aria-hidden', 'true');
  scene.innerHTML = `
    <svg class="dimension-blueprint" viewBox="0 0 1200 900" preserveAspectRatio="xMidYMid slice" aria-hidden="true">
      <g class="orbit-spin">
        <circle class="orbit-line" cx="830" cy="300" r="246"/>
        <circle class="orbit-line dashed" cx="830" cy="300" r="284"/>
        <path class="orbit-line" d="M520 338 Q790 36 1138 252 Q942 594 626 526"/>
      </g>
      <g class="orbit-spin-reverse">
        <ellipse class="orbit-line ghost" cx="830" cy="300" rx="390" ry="138" transform="rotate(-24 830 300)"/>
        <ellipse class="orbit-line" cx="830" cy="300" rx="318" ry="94" transform="rotate(38 830 300)"/>
      </g>
      <path class="orbit-line ghost" d="M-80 780 Q308 92 1040 48 Q742 474 1240 860"/>
      <path class="orbit-line" d="M64 824 Q418 588 1142 726"/>
      <path class="orbit-line dashed" d="M86 94 Q342 322 1022 712"/>
      <line class="orbit-line ghost" x1="190" y1="0" x2="482" y2="900"/>
      <line class="orbit-line ghost" x1="0" y1="620" x2="1200" y2="438"/>
      <circle class="orbit-line" cx="177" cy="632" r="7"/>
      <circle class="orbit-line" cx="520" cy="676" r="18"/>
      <circle class="orbit-line ghost" cx="1060" cy="628" r="44"/>
    </svg>
    <div class="dimension-disc"></div>
    <i class="dimension-node node-a"></i>
    <i class="dimension-node node-b"></i>
    <i class="dimension-node node-c"></i>
    <svg class="dimension-flight flight-a" viewBox="0 0 90 50" aria-hidden="true"><path d="M0 25Q37-4 88 4Q52 10 37 45Q27 20 0 25Z"/></svg>
    <svg class="dimension-flight flight-b" viewBox="0 0 90 50" aria-hidden="true"><path d="M0 25Q37-4 88 4Q52 10 37 45Q27 20 0 25Z"/></svg>
    <svg class="dimension-flight flight-c" viewBox="0 0 90 50" aria-hidden="true"><path d="M0 25Q37-4 88 4Q52 10 37 45Q27 20 0 25Z"/></svg>
    <div class="dimension-index">HUMAN / AI<br>OBSERVATION 001</div>
    <div class="dimension-caption"><b>Turing</b><span>Human · Machine<br>Observe · Discern</span></div>`;
  document.body.append(scene);

  function currentPage() {
    const visible = [...document.querySelectorAll('.page')].find(page => page.style.display !== 'none');
    return visible ? visible.id.replace('page-', '') : 'login';
  }

  document.body.dataset.view = currentPage();

  function animateTarget(selector) {
    if (reducedMotion.matches) return;
    const target = document.querySelector(selector);
    if (!target || !target.animate) return;
    target.animate([
      {opacity: 0, transform: 'translate3d(24px, 16px, -36px) rotateY(-2deg)', filter: 'blur(5px)'},
      {opacity: 1, transform: 'translate3d(0, 0, 0) rotateY(0)', filter: 'blur(0)'}
    ], {duration: 520, easing: 'cubic-bezier(.16,1,.3,1)', fill: 'both'});
  }

  function wrapTransition(name, targetFor) {
    const original = window[name];
    if (typeof original !== 'function') return;

    window[name] = function dimensionTransition(...args) {
      let result;
      const update = () => {
        result = original.apply(this, args);
        if (name === 'switchPage') document.body.dataset.view = args[0] || currentPage();
      };

      if (reducedMotion.matches) {
        update();
      } else if (document.startViewTransition) {
        const transition = document.startViewTransition(update);
        transition.ready.catch(() => {});
        transition.finished.catch(() => {});
      } else {
        update();
        requestAnimationFrame(() => animateTarget(targetFor(args)));
      }
      return result;
    };
  }

  wrapTransition('switchPage', args => `#page-${args[0]}`);
  wrapTransition('switchLoginTab', args => `#form-${args[0]}`);
  wrapTransition('switchRoomTab', args => `#room-${args[0]}`);

  const orbitNav = document.getElementById('orbit-navigation');
  const orbitCore = document.getElementById('core-entry');
  const authSheet = document.getElementById('auth-sheet');
  const authOther = document.getElementById('auth-other');
  let activeAuthMode = 'login';

  function closeAuth() {
    if (!authSheet) return;
    authSheet.hidden = true;
    orbitNav?.classList.remove('auth-visible');
  }

  function openAuth(mode) {
    if (!authSheet) return;
    activeAuthMode = mode === 'register' ? 'register' : 'login';
    authSheet.hidden = false;
    orbitNav?.classList.add('auth-visible');
    document.getElementById('auth-overline').textContent = 'OBSERVER ACCESS · 观测者入口';
    document.getElementById('auth-title').innerHTML = activeAuthMode === 'login'
      ? '登录 <span>Login</span>'
      : '建立档案 <span>Register</span>';
    document.getElementById('auth-description').textContent = activeAuthMode === 'login'
      ? '沿着星轨返回你的观察档案。'
      : '建立一份新的观测档案，进入图灵实验。';
    if (authOther) {
      authOther.textContent = activeAuthMode === 'login'
        ? '没有档案？前往注册 · Register'
        : '已有档案？返回登录 · Login';
    }
    window.switchLoginTab(activeAuthMode);
    const input = document.getElementById(activeAuthMode === 'login' ? 'login-name' : 'reg-name');
    requestAnimationFrame(() => input?.focus({preventScroll: true}));
  }

  document.querySelectorAll('[data-auth]').forEach(entry => {
    entry.addEventListener('click', () => openAuth(entry.dataset.auth));
  });
  orbitCore?.addEventListener('click', () => openAuth('login'));
  document.getElementById('auth-close')?.addEventListener('click', closeAuth);
  authOther?.addEventListener('click', () => openAuth(activeAuthMode === 'login' ? 'register' : 'login'));
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') closeAuth();
  });

  if (orbitNav) {
    orbitNav.addEventListener('pointermove', event => {
      if (reducedMotion.matches || !finePointer.matches) return;
      const rect = orbitNav.getBoundingClientRect();
      const x = (event.clientX - rect.left) / Math.max(rect.width, 1) - .5;
      const y = (event.clientY - rect.top) / Math.max(rect.height, 1) - .5;
      orbitCore?.style.setProperty('--core-x', `${x * 9}px`);
      orbitCore?.style.setProperty('--core-y', `${y * 7}px`);
    }, {passive: true});
    orbitNav.addEventListener('pointerleave', () => {
      orbitCore?.style.setProperty('--core-x', '0px');
      orbitCore?.style.setProperty('--core-y', '0px');
    });
  }

  function updateManuscriptTime() {
    const date = new Date();
    const timeText = date.toLocaleTimeString('en-GB', {hour: '2-digit', minute: '2-digit'});
    const dateText = date.toLocaleDateString('en-GB', {weekday: 'short', day: '2-digit', month: 'short'}).toUpperCase().replace(/,/g, ' ·');
    ['manuscript-time', 'hub-time'].forEach(id => {
      const time = document.getElementById(id);
      if (time) time.textContent = timeText;
    });
    ['manuscript-date', 'hub-date'].forEach(id => {
      const label = document.getElementById(id);
      if (label) label.textContent = dateText;
    });
  }
  updateManuscriptTime();
  setInterval(updateManuscriptTime, 60000);

  let frame = 0;
  let pointerX = 0;
  let pointerY = 0;
  let tiltedCard = null;

  function resetCard(card) {
    if (!card) return;
    card.style.setProperty('--card-rx', '0deg');
    card.style.setProperty('--card-ry', '0deg');
  }

  function renderPointerScene() {
    frame = 0;
    scene.style.setProperty('--mx', `${pointerX * 28}px`);
    scene.style.setProperty('--my', `${pointerY * 22}px`);
    scene.style.setProperty('--mx-reverse', `${pointerX * -18}px`);
    scene.style.setProperty('--my-reverse', `${pointerY * -14}px`);
  }

  document.addEventListener('pointermove', event => {
    if (reducedMotion.matches || !finePointer.matches) return;

    pointerX = event.clientX / Math.max(window.innerWidth, 1) - .5;
    pointerY = event.clientY / Math.max(window.innerHeight, 1) - .5;
    if (!frame) frame = requestAnimationFrame(renderPointerScene);

    const card = event.target.closest ? event.target.closest('.card') : null;
    if (tiltedCard && tiltedCard !== card) resetCard(tiltedCard);
    tiltedCard = card;
    if (!card) return;

    const rect = card.getBoundingClientRect();
    const x = (event.clientX - rect.left) / Math.max(rect.width, 1);
    const y = (event.clientY - rect.top) / Math.max(rect.height, 1);
    card.style.setProperty('--card-rx', `${(.5 - y) * 2.4}deg`);
    card.style.setProperty('--card-ry', `${(x - .5) * 3.2}deg`);
  }, {passive: true});

  document.addEventListener('pointerout', event => {
    const card = event.target.closest ? event.target.closest('.card') : null;
    if (card && !card.contains(event.relatedTarget)) {
      resetCard(card);
      if (tiltedCard === card) tiltedCard = null;
    }
  }, {passive: true});

  document.documentElement.addEventListener('mouseleave', () => {
    resetCard(tiltedCard);
    tiltedCard = null;
    pointerX = 0;
    pointerY = 0;
    if (!frame) frame = requestAnimationFrame(renderPointerScene);
  });

  reducedMotion.addEventListener('change', () => {
    if (!reducedMotion.matches) return;
    resetCard(tiltedCard);
    tiltedCard = null;
    scene.style.removeProperty('--mx');
    scene.style.removeProperty('--my');
    scene.style.removeProperty('--mx-reverse');
    scene.style.removeProperty('--my-reverse');
  });

  window.turingDimensionUI = Object.freeze({
    version: '2.2.0',
    refreshView() { document.body.dataset.view = currentPage(); }
  });
})();
