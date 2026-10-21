import{n as e,r as t,t as n}from"./jsx-runtime--AhPlkRw.js";import{n as r,t as i}from"./snowflake-D14Zswox.js";var a={name:`lock`,size:24,node:[[`rect`,{width:`18`,height:`11`,x:`3`,y:`11`,rx:`2`,ry:`2`,key:`1w4ew1`}],[`path`,{d:`M7 11V7a5 5 0 0 1 10 0v4`,key:`fwvmzm`}]]};a.node;var o=r(a),s={name:`mail`,size:24,node:[[`path`,{d:`m22 7-8.991 5.727a2 2 0 0 1-2.009 0L2 7`,key:`132q7q`}],[`rect`,{x:`2`,y:`4`,width:`20`,height:`16`,rx:`2`,key:`izxlao`}]]};s.node;var c=r(s),l=e(),u=t(),d=n();function f({portal:e=`user`,failed:t=!1,denied:n=!1}){let[r,i]=(0,u.useState)(e),a=r===`admin`,o=r=>r===e?n?`That account isn't an admin. Use the user sign in.`:t?`That email and password don't match.`:null:null;return(0,d.jsxs)(`div`,{className:`auth-switch`,children:[(0,d.jsx)(`style`,{children:_}),(0,d.jsx)(g,{}),(0,d.jsxs)(`div`,{className:a?`as-container access-mode`:`as-container`,children:[(0,d.jsx)(`div`,{className:`as-forms`,children:(0,d.jsxs)(`div`,{className:`as-signin-access`,children:[(0,d.jsx)(p,{className:`as-signin`,portal:`user`,title:`User sign in`,subtitle:`Talk to the IT Help Desk assistant`,error:o(`user`),hidden:a}),(0,d.jsx)(p,{className:`as-access`,portal:`admin`,title:`Admin sign in`,subtitle:`Ops console for the support team`,error:o(`admin`),hidden:!a})]})}),(0,d.jsxs)(`div`,{className:`as-panels`,children:[(0,d.jsx)(`div`,{className:`as-panel as-left`,children:(0,d.jsxs)(`div`,{className:`as-content`,children:[(0,d.jsx)(m,{}),(0,d.jsx)(`h3`,{children:`Support team?`}),(0,d.jsx)(`p`,{children:`Admins sign in here to open the ops console.`}),(0,d.jsx)(`button`,{type:`button`,className:`as-btn as-ghost`,onClick:()=>i(`admin`),children:`Admin login`})]})}),(0,d.jsx)(`div`,{className:`as-panel as-right`,children:(0,d.jsxs)(`div`,{className:`as-content`,children:[(0,d.jsx)(m,{}),(0,d.jsx)(`h3`,{children:`Need IT help?`}),(0,d.jsx)(`p`,{children:`Users sign in here to chat with the assistant.`}),(0,d.jsx)(`button`,{type:`button`,className:`as-btn as-ghost`,onClick:()=>i(`user`),children:`User login`})]})})]})]})]})}function p({className:e,portal:t,title:n,subtitle:r,error:a,hidden:s}){return(0,d.jsxs)(`form`,{className:e,method:`post`,action:`/login`,inert:s,children:[(0,d.jsx)(`input`,{type:`hidden`,name:`portal`,value:t}),(0,d.jsxs)(`p`,{className:`as-wordmark`,children:[(0,d.jsx)(i,{size:20,"aria-hidden":!0}),`SNOW AGENT`]}),(0,d.jsx)(`h2`,{className:`as-title`,children:n}),(0,d.jsx)(`p`,{className:`as-subtitle`,children:r}),(0,d.jsxs)(`label`,{className:`as-field`,children:[(0,d.jsx)(c,{size:18,"aria-hidden":!0}),(0,d.jsx)(`input`,{name:`email`,type:`email`,placeholder:`Email`,"aria-label":`Email`,autoComplete:`username`,required:!0,autoFocus:!s})]}),(0,d.jsxs)(`label`,{className:`as-field`,children:[(0,d.jsx)(o,{size:18,"aria-hidden":!0}),(0,d.jsx)(`input`,{name:`password`,type:`password`,placeholder:`Password`,"aria-label":`Password`,autoComplete:`current-password`,required:!0})]}),a&&(0,d.jsx)(`p`,{className:`as-error`,role:`alert`,children:a}),(0,d.jsx)(`button`,{type:`submit`,className:`as-btn`,children:`Sign in`})]})}function m(){return(0,d.jsxs)(`div`,{className:`as-brand`,children:[(0,d.jsx)(i,{size:56,strokeWidth:1.4,"aria-hidden":!0,className:`as-brand-flake`}),(0,d.jsx)(`span`,{children:`SNOW AGENT`})]})}var h=Array.from({length:48},(e,t)=>({left:t*37%100,size:3+t*7%5,duration:9+t*13%11,delay:-(t*17%20),drift:(t%5-2)*18,opacity:.35+t*11%6/10}));function g(){return(0,d.jsx)(`div`,{className:`as-snow`,"aria-hidden":!0,children:h.map((e,t)=>(0,d.jsx)(`span`,{style:{left:`${e.left}%`,width:e.size,height:e.size,opacity:e.opacity,animationDuration:`${e.duration}s`,animationDelay:`${e.delay}s`,"--drift":`${e.drift}px`}},t))})}var _=`
.auth-switch {
  --as-a: #2f7fd1;
  --as-b: #0d2b52;
  --as-ice: #e8f3fc;
  min-height: 100vh;
  width: 100%;
  display: flex;
  justify-content: center;
  align-items: center;
  padding: 20px;
  position: relative;
  overflow: hidden;
  background:
    radial-gradient(ellipse at 50% 120%, rgba(186, 225, 255, 0.35), transparent 60%),
    linear-gradient(170deg, #07172b 0%, #0d2b52 55%, #1b4f86 100%);
}
.auth-switch .as-snow { position: absolute; inset: 0; pointer-events: none; z-index: 0; }
.auth-switch .as-snow span {
  position: absolute;
  top: -10px;
  border-radius: 50%;
  background: #fff;
  box-shadow: 0 0 6px rgba(255, 255, 255, 0.8);
  animation-name: as-fall;
  animation-timing-function: linear;
  animation-iteration-count: infinite;
}
@keyframes as-fall {
  to { transform: translate(var(--drift), 105vh); }
}
.auth-switch .as-container {
  position: relative;
  width: 100%;
  max-width: 900px;
  height: 550px;
  background: #fff;
  z-index: 1;
  border-radius: 20px;
  box-shadow: 0 25px 60px rgba(0, 10, 30, 0.5), 0 0 0 1px rgba(255, 255, 255, 0.15);
  overflow: hidden;
}
.auth-switch .as-container::before {
  content: "";
  position: absolute;
  height: 2000px;
  width: 2000px;
  top: -10%;
  right: 48%;
  transform: translateY(-50%);
  background: linear-gradient(-45deg, #4fa3ec 0%, var(--as-a) 35%, var(--as-b) 100%);
  transition: 1.8s ease-in-out;
  border-radius: 50%;
  z-index: 6;
}
.auth-switch .as-forms {
  position: absolute;
  inset: 0;
}
.auth-switch .as-signin-access {
  position: absolute;
  top: 50%;
  left: 75%;
  width: 50%;
  transform: translate(-50%, -50%);
  transition: 1s 0.7s ease-in-out;
  display: grid;
  grid-template-columns: 1fr;
  z-index: 5;
}
.auth-switch .as-signin,
.auth-switch .as-access {
  grid-column: 1 / 2;
  grid-row: 1 / 2;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 0 4rem;
  transition: all 0.2s 0.7s;
  overflow: hidden;
}
.auth-switch .as-signin { z-index: 2; }
.auth-switch .as-access { opacity: 0; z-index: 1; }

.auth-switch .as-title {
  font-size: 2rem;
  color: #0d2b52;
  text-align: center;
  margin: 0 0 4px;
  font-weight: 700;
}
.auth-switch .as-subtitle {
  margin: 0 0 14px;
  color: #666;
  font-size: 0.95rem;
  text-align: center;
}
.auth-switch .as-field {
  max-width: 380px;
  width: 100%;
  height: 55px;
  margin: 8px 0;
  padding: 0 1.2rem;
  display: flex;
  align-items: center;
  gap: 12px;
  background: var(--as-ice);
  border-radius: 55px;
  color: #666;
  transition: 0.3s;
}
.auth-switch .as-field:focus-within {
  background: #dcecfa;
  box-shadow: 0 0 0 2px var(--as-a);
}
.auth-switch .as-field input {
  flex: 1;
  min-width: 0;
  background: none;
  border: none;
  outline: none;
  padding: 0;
  font-size: 1rem;
  font-weight: 500;
  color: #333;
}
.auth-switch .as-field input::placeholder { color: #999; font-weight: 400; }
.auth-switch .as-error {
  margin: 6px 0 0;
  color: #c0392b;
  font-size: 0.9rem;
}
.auth-switch .as-btn {
  width: 150px;
  height: 49px;
  margin: 12px 0;
  border: none;
  border-radius: 49px;
  background: var(--as-a);
  color: #fff;
  font-weight: 600;
  font-size: 0.9rem;
  text-transform: uppercase;
  cursor: pointer;
  transition: 0.5s;
}
.auth-switch .as-btn:hover {
  background: #1f6bb8;
  transform: translateY(-2px);
  box-shadow: 0 5px 15px rgba(47, 127, 209, 0.4);
}
.auth-switch .as-btn:focus-visible { outline: 2px solid #fff; outline-offset: 2px; box-shadow: 0 0 0 4px var(--as-a); }

.auth-switch .as-panels {
  position: absolute;
  inset: 0;
  display: grid;
  grid-template-columns: repeat(2, 1fr);
}
.auth-switch .as-panel {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  justify-content: space-around;
  text-align: center;
  z-index: 6;
}
.auth-switch .as-left { pointer-events: all; padding: 3rem 17% 2rem 12%; }
.auth-switch .as-right { pointer-events: none; padding: 3rem 12% 2rem 17%; }
.auth-switch .as-content {
  color: #fff;
  transition: transform 0.9s ease-in-out 0.6s;
}
.auth-switch .as-wordmark {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 0 10px;
  color: var(--as-a);
  font-size: 0.85rem;
  font-weight: 700;
  letter-spacing: 0.22em;
}
.auth-switch .as-brand {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  margin-bottom: 28px;
  font-size: 1.6rem;
  font-weight: 800;
  letter-spacing: 0.18em;
  text-shadow: 0 2px 12px rgba(0, 20, 50, 0.35);
}
.auth-switch .as-brand-flake {
  filter: drop-shadow(0 0 10px rgba(200, 235, 255, 0.8));
  animation: as-spin 24s linear infinite;
}
@keyframes as-spin { to { transform: rotate(360deg); } }
.auth-switch .as-panel h3 { margin: 0 0 10px; font-size: 1.5rem; font-weight: 600; line-height: 1; }
.auth-switch .as-panel p { margin: 0; padding: 0.7rem 0; font-size: 0.95rem; }
.auth-switch .as-btn.as-ghost {
  margin: 0;
  width: 130px;
  height: 41px;
  background: none;
  border: 2px solid #fff;
  font-size: 0.8rem;
}
.auth-switch .as-btn.as-ghost:hover { background: rgba(255, 255, 255, 0.1); box-shadow: none; }
.auth-switch .as-right .as-content { transform: translateX(800px); }

.auth-switch .access-mode::before { transform: translate(100%, -50%); right: 52%; }
.auth-switch .access-mode .as-left .as-content { transform: translateX(-800px); }
.auth-switch .access-mode .as-signin-access { left: 25%; }
.auth-switch .access-mode .as-access { opacity: 1; z-index: 2; }
.auth-switch .access-mode .as-signin { opacity: 0; z-index: 1; }
.auth-switch .access-mode .as-right .as-content { transform: translateX(0); }
.auth-switch .access-mode .as-left { pointer-events: none; }
.auth-switch .access-mode .as-right { pointer-events: all; }

@media (max-width: 870px) {
  .auth-switch .as-container { min-height: 800px; height: 100vh; }
  .auth-switch .as-signin-access {
    width: 100%;
    top: 95%;
    transform: translate(-50%, -100%);
    transition: 1s 0.8s ease-in-out;
  }
  .auth-switch .as-signin-access,
  .auth-switch .access-mode .as-signin-access { left: 50%; }
  .auth-switch .as-panels { grid-template-columns: 1fr; grid-template-rows: 1fr 2fr 1fr; }
  .auth-switch .as-panel {
    flex-direction: row;
    justify-content: space-around;
    align-items: center;
    padding: 2.5rem 8%;
    grid-column: 1 / 2;
  }
  .auth-switch .as-right { grid-row: 3 / 4; }
  .auth-switch .as-left { grid-row: 1 / 2; }
  .auth-switch .as-content { padding-right: 15%; transition: transform 0.9s ease-in-out 0.8s; }
  .auth-switch .as-panel h3 { font-size: 1.2rem; }
  .auth-switch .as-brand { flex-direction: row; font-size: 1rem; margin-bottom: 8px; }
  .auth-switch .as-brand-flake { width: 24px; height: 24px; }
  .auth-switch .as-panel p { font-size: 0.75rem; padding: 0.5rem 0; }
  .auth-switch .as-btn.as-ghost { width: 110px; height: 35px; font-size: 0.7rem; }
  .auth-switch .as-container::before {
    width: 1500px;
    height: 1500px;
    transform: translateX(-50%);
    left: 30%;
    bottom: 68%;
    right: initial;
    top: initial;
    transition: 2s ease-in-out;
  }
  .auth-switch .access-mode::before { transform: translate(-50%, 100%); bottom: 32%; right: initial; }
  .auth-switch .access-mode .as-left .as-content { transform: translateY(-300px); }
  .auth-switch .access-mode .as-right .as-content { transform: translateY(0); }
  .auth-switch .as-right .as-content { transform: translateY(300px); }
  .auth-switch .access-mode .as-signin-access { top: 5%; transform: translate(-50%, 0); }
}

@media (max-width: 570px) {
  .auth-switch { padding: 0; }
  .auth-switch .as-container { border-radius: 0; }
  .auth-switch .as-signin,
  .auth-switch .as-access { padding: 0 1.5rem; }
  .auth-switch .as-content { padding: 0.5rem 1rem; }
}

@media (prefers-reduced-motion: reduce) {
  .auth-switch *,
  .auth-switch *::before { transition-duration: 0.01s !important; transition-delay: 0s !important; }
  .auth-switch .as-snow { display: none; }
  .auth-switch .as-brand-flake { animation: none; }
}
`;function v(){let e=new URLSearchParams(location.search);return(0,d.jsx)(f,{portal:e.get(`portal`)===`admin`?`admin`:`user`,failed:e.has(`failed`),denied:e.has(`denied`)})}(0,l.createRoot)(document.getElementById(`root`)).render((0,d.jsx)(v,{}));