"use strict";

const $ = (id) => document.getElementById(id);

// 면접 단계: asking(질문 제시) → answering(답변) → processing(서버 대기) → 반복 → done
const state = {
  setup: null,
  schoolKey: null,
  sessionId: null,
  totalLimitMs: 0,
  answerLimitMs: 0,
  phase: "setup",
  activeMs: 0, // 전체 타이머. 서버를 기다리는 시간은 세지 않는다.
  answerMs: 0, // 이번 답변에 쓴 시간
  lastTick: 0,
  tickId: null,
  pending: null, // 전송 중인 답변. 실패하면 같은 내용을 다시 보낸다.
  question: null,
  review: null, // 종료 화면에 그릴 세션 기록
  mode: "voice", // voice | text
  stream: null, // 면접 동안 열어 두는 마이크
  recorder: null,
  chunks: [],
  audioContext: null,
  analyser: null,
  drill: false, // 실전 모드: 질문을 글로 보여주지 않는다
  replayUsed: false, // 이번 질문에서 다시 듣기를 썼는지
};

// ---------- 공통 ----------

async function api(path, options) {
  let res;
  try {
    res = await fetch(path, options);
  } catch {
    throw new Error("서버에 연결하지 못했습니다. 서버가 켜져 있는지 확인하세요.");
  }
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = body && body.detail;
    const message = typeof detail === "string" ? detail : `요청이 실패했습니다 (HTTP ${res.status}).`;
    throw new Error(message);
  }
  return body;
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else node.setAttribute(key, value);
  }
  node.append(...children);
  return node;
}

function showScreen(name) {
  for (const screen of ["setup", "interview", "done"]) {
    $(`screen-${screen}`).hidden = screen !== name;
  }
  window.scrollTo(0, 0);
}

function showError(id, message) {
  const node = $(id);
  node.hidden = !message;
  if (message) node.textContent = message;
}

function clock(ms) {
  const sec = Math.max(0, Math.ceil(ms / 1000));
  return `${String(Math.floor(sec / 60)).padStart(2, "0")}:${String(sec % 60).padStart(2, "0")}`;
}

function duration(sec) {
  const s = Math.round(sec);
  return s >= 60 ? `${Math.floor(s / 60)}분 ${s % 60}초` : `${s}초`;
}

// ---------- 음성 출력 ----------

const tts = { available: false, voices: { A: null, B: null }, current: null };
// 같은 목소리밖에 없을 때도 두 면접위원이 구분되도록 높낮이를 달리한다.
const PITCH = { A: 1.0, B: 0.75 };
// 질문이 끝난 뒤 스피커 잔향이 녹음에 들어가지 않도록 잠깐 기다린다.
const AFTER_SPEECH_MS = 250;
const CLOSING_REMARK = "수고하셨습니다. 면접을 마치겠습니다.";

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function loadVoices() {
  if (!("speechSynthesis" in window)) return;
  const korean = speechSynthesis
    .getVoices()
    .filter((voice) => voice.lang.replace("_", "-").toLowerCase().startsWith("ko"));
  // 자연스러운(Natural) 목소리가 있으면 먼저 쓴다.
  korean.sort((a, b) => Number(/natural/i.test(b.name)) - Number(/natural/i.test(a.name)));
  tts.available = korean.length > 0;
  tts.voices.A = korean[0] || null;
  tts.voices.B = korean[1] || korean[0] || null;
  const status = $("tts-status");
  if (!tts.available) {
    status.textContent = "브라우저에 한국어 음성이 없어 질문을 글로 보여줍니다.";
  } else if (tts.voices.A === tts.voices.B) {
    status.textContent = `한국어 음성 1개(${tts.voices.A.name}). 면접위원 A와 B는 목소리 높낮이로 구분합니다.`;
  } else {
    status.textContent = `면접위원 A: ${tts.voices.A.name} / 면접위원 B: ${tts.voices.B.name}`;
  }
  $("tts-test-button").disabled = !tts.available;
}

// 다 읽으면 true, 읽지 못했으면 false. 끝났다는 신호가 오지 않는 브라우저에 대비해
// 글자 수에 비례한 제한 시간을 둔다.
function speak(text, who) {
  if (!tts.available) return Promise.resolve(false);
  speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = "ko-KR";
  utterance.voice = tts.voices[who];
  // 목소리가 서로 다르면 원래 높낮이 그대로 읽는다. 높낮이를 바꾸면 기계음처럼 들린다.
  utterance.pitch = tts.voices.A === tts.voices.B ? PITCH[who] : 1;
  tts.current = utterance; // 참조를 잡아 두지 않으면 읽는 도중에 수거되는 브라우저가 있다
  return new Promise((resolve) => {
    let settled = false;
    const finish = (ok) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(ok);
    };
    const timer = setTimeout(() => {
      speechSynthesis.cancel();
      finish(true);
    }, 4000 + text.length * 350);
    utterance.addEventListener("end", () => finish(true));
    utterance.addEventListener("error", () => finish(false));
    speechSynthesis.speak(utterance);
  });
}

function stopSpeaking() {
  if ("speechSynthesis" in window) speechSynthesis.cancel();
}

async function runTtsTest() {
  const button = $("tts-test-button");
  button.disabled = true;
  await speak("면접위원 에이입니다. 자리에 앉으세요.", "A");
  await speak("면접위원 비입니다. 편하게 답변하시면 됩니다.", "B");
  button.disabled = false;
}

// ---------- 마이크와 녹음 ----------

async function openMic() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    throw new Error("이 브라우저에서는 마이크를 쓸 수 없습니다. http://localhost 주소로 열었는지 확인하세요.");
  }
  try {
    return await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true },
    });
  } catch (err) {
    const denied = err.name === "NotAllowedError" || err.name === "SecurityError";
    throw new Error(denied ? "마이크 권한이 거부됐습니다." : "마이크를 찾지 못했습니다.");
  }
}

function closeMic() {
  if (state.stream) state.stream.getTracks().forEach((track) => track.stop());
  if (state.audioContext) state.audioContext.close();
  state.stream = null;
  state.recorder = null;
  state.audioContext = null;
  state.analyser = null;
}

function startRecording(stream) {
  const preferred = "audio/webm;codecs=opus";
  const options = MediaRecorder.isTypeSupported(preferred) ? { mimeType: preferred } : {};
  const recorder = new MediaRecorder(stream, options);
  const chunks = [];
  recorder.addEventListener("dataavailable", (event) => {
    if (event.data.size) chunks.push(event.data);
  });
  recorder.start();
  recorder.result = new Promise((resolve) => {
    recorder.addEventListener("stop", () => resolve(new Blob(chunks, { type: recorder.mimeType })));
  });
  return recorder;
}

async function stopRecording(recorder) {
  if (recorder.state !== "inactive") recorder.stop();
  return recorder.result;
}

function micLevel() {
  if (!state.analyser) return 0;
  const samples = new Float32Array(state.analyser.fftSize);
  state.analyser.getFloatTimeDomainData(samples);
  let sum = 0;
  for (const value of samples) sum += value * value;
  // 보통 말소리의 RMS는 0.02~0.2다. 막대가 잘 보이도록 키운다.
  return Math.min(1, Math.sqrt(sum / samples.length) * 6);
}

async function runMicTest() {
  const button = $("mic-test-button");
  const result = $("mic-test-result");
  button.disabled = true;
  let stream;
  try {
    stream = await openMic();
    result.textContent = "녹음 중… 아무 말이나 해 보세요.";
    const recorder = startRecording(stream);
    await new Promise((resolve) => setTimeout(resolve, 3000));
    const blob = await stopRecording(recorder);
    result.textContent = "받아 적는 중…";
    const form = new FormData();
    form.set("audio", blob, "test.webm");
    const res = await api("/api/stt/test", { method: "POST", body: form });
    result.textContent = res.transcript
      ? `인식 결과: ${res.transcript}`
      : "말소리가 들리지 않았습니다. 마이크 입력 장치와 음량을 확인하세요.";
  } catch (err) {
    result.textContent = err.message;
  } finally {
    if (stream) stream.getTracks().forEach((track) => track.stop());
    button.disabled = false;
  }
}

function setMode(mode, notice = "") {
  state.mode = mode;
  $(`mode-${mode}`).checked = true;
  $("mic-test").hidden = mode !== "voice";
  const node = $("mode-notice");
  node.hidden = !notice;
  node.textContent = notice;
}

// ---------- 설정 화면 ----------

async function loadSetup() {
  try {
    state.setup = await api("/api/setup");
  } catch (err) {
    $("setup-loading").textContent = err.message;
    return;
  }
  const setup = state.setup;
  $("setup-loading").hidden = true;
  $("setup-body").hidden = false;

  const list = $("school-list");
  list.replaceChildren(
    ...setup.schools.map((school, i) => {
      const input = el("input", { type: "radio", name: "school", value: school.key, id: `school-${school.key}` });
      input.checked = i === 0;
      input.addEventListener("change", () => selectSchool(school.key));
      return el(
        "label",
        { class: "school", for: input.id },
        input,
        el("span", { class: "school-name", text: school.university }),
        el("span", { class: "school-track", text: `${school.track} · ${school.department}` }),
      );
    }),
  );
  selectSchool(setup.schools[0].key);

  const limit = $("answer-limit");
  limit.min = setup.answer_limit.min;
  limit.max = setup.answer_limit.max;
  limit.value = setup.answer_limit.default;

  renderRecord(setup);

  const stt = setup.stt;
  if (stt.error) {
    $("stt-status").textContent = "";
    $("mode-voice").disabled = true;
    setMode("text", "음성 인식 모델을 올리지 못해 글로만 답변할 수 있습니다.");
  } else {
    $("stt-status").textContent = stt.loaded
      ? `음성 인식: ${stt.model} (${stt.device === "cuda" ? "GPU" : "CPU"})`
      : `음성 인식 모델(${stt.model})을 올리는 중입니다. 첫 전사가 조금 늦을 수 있습니다.`;
    setMode("voice");
  }

  const problems = [];
  if (!setup.api_key_set) problems.push(".env에 ANTHROPIC_API_KEY가 없습니다.");
  if (!setup.record.ok) problems.push(setup.record.error);
  showError("setup-error", problems.join(" "));
  $("start-button").disabled = problems.length > 0;
}

function selectSchool(key) {
  state.schoolKey = key;
  const school = state.setup.schools.find((s) => s.key === key);
  const weights = school.competencies.map((c) => `${c.name} ${Math.round(c.weight)}%`).join(", ");
  const rows = [
    ["면접 시간", `${school.time_note} (타이머 ${duration(school.total_limit_sec)})`],
    ["면접위원", `${school.interviewer_count}명`],
    ["평가요소", weights],
  ];
  $("school-detail").replaceChildren(
    ...rows.flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })]),
  );
}

function renderRecord(setup) {
  const record = setup.record;
  if (!record.ok) {
    $("record-summary").textContent = "생기부를 읽지 못했습니다.";
    $("record-files").replaceChildren();
    return;
  }
  $("record-summary").textContent =
    `data/records/의 파일 ${record.sources.length}개를 하나의 생기부로 사용합니다. ` +
    `전체 ${record.chars.toLocaleString()}자.`;
  $("record-files").replaceChildren(
    ...record.sources.map((src) => {
      let detail = `${src.chars.toLocaleString()}자`;
      if (src.kind === "pdf") {
        const [from, to] = src.pages_read;
        detail = `${src.pages_total}쪽 중 ${from}~${to}쪽 읽음, ${detail}`;
      } else if (src.kind === "image") {
        detail = `이미지 전사본, ${detail}`;
      }
      const item = el("li", {}, el("strong", { text: src.name }), el("span", { class: "muted", text: ` · ${detail}` }));
      if (src.kind === "image") {
        item.append(
          el(
            "details",
            {},
            el("summary", { text: "전사본 펼쳐 보기" }),
            el("pre", { text: src.transcript }),
            el("p", {
              class: "muted small",
              text: `틀린 곳이 있으면 data/records/.cache/${src.name}.txt를 직접 고치세요.`,
            }),
          ),
        );
      }
      return item;
    }),
  );

  const missing = [];
  if (!setup.mask.name_set) missing.push("이름(MASK_NAME)");
  if (!setup.mask.school_set) missing.push("학교명(MASK_SCHOOL)");
  const warning = $("mask-warning");
  warning.hidden = missing.length === 0;
  warning.textContent =
    `.env에 ${missing.join(", ")}이 없어 생기부에 있는 그대로 외부 API로 전송됩니다.`;
}

async function startInterview() {
  const limit = $("answer-limit");
  const answerLimit = Number(limit.value);
  if (!Number.isInteger(answerLimit) || answerLimit < Number(limit.min) || answerLimit > Number(limit.max)) {
    showError("setup-error", `답변 제한 시간은 ${limit.min}~${limit.max}초 사이의 정수로 입력하세요.`);
    return;
  }
  showError("setup-error", "");
  stopSpeaking();
  state.drill = $("drill-mode").checked;
  const button = $("start-button");
  button.disabled = true;
  if (state.mode === "voice") {
    try {
      state.stream = await openMic();
    } catch (err) {
      // 마이크를 못 쓰면 같은 타이머로 글 답변을 받는다.
      setMode("text", `${err.message} 이번 면접은 글로 답변합니다.`);
    }
  }
  button.textContent = "첫 질문을 준비하는 중…";
  try {
    const res = await api("/api/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ school: state.schoolKey, answer_limit_sec: answerLimit }),
    });
    beginSession(res);
  } catch (err) {
    closeMic();
    showError("setup-error", err.message);
  } finally {
    button.disabled = false;
    button.textContent = "면접 시작";
  }
}

// ---------- 면접 화면 ----------

function beginSession(res) {
  const school = state.setup.schools.find((s) => s.key === state.schoolKey);
  state.sessionId = res.session_id;
  state.totalLimitMs = res.total_limit_sec * 1000;
  state.answerLimitMs = res.answer_limit_sec * 1000;
  state.activeMs = 0;
  state.answerMs = 0;
  state.pending = null;
  $("interview-school").textContent = `${school.university} · ${school.department}`;
  $("interview-error").hidden = true;
  const voice = state.mode === "voice";
  $("voice-input").hidden = !voice;
  $("answer-text").hidden = voice;
  $("submit-hint").hidden = voice;
  if (voice) {
    state.audioContext = new AudioContext();
    state.analyser = state.audioContext.createAnalyser();
    state.analyser.fftSize = 1024;
    state.audioContext.createMediaStreamSource(state.stream).connect(state.analyser);
  }
  showScreen("interview");

  state.lastTick = performance.now();
  clearInterval(state.tickId);
  state.tickId = setInterval(tick, 200);
  presentQuestion(res.question);
}

function setPhase(phase) {
  state.phase = phase;
  const answering = phase === "answering";
  $("answer-text").disabled = !answering;
  $("submit-button").disabled = !answering;
  $("replay-button").disabled = !answering || state.replayUsed;
  const waiting = state.mode === "voice" ? "답변을 받아 적고 다음 질문을 준비하는 중입니다." : "다음 질문을 준비하는 중입니다.";
  let note = "";
  if (phase === "processing") note = `${waiting} 이 시간은 면접 시간에서 빠집니다.`;
  else if (phase === "asking" && tts.available) note = "면접위원이 질문하고 있습니다.";
  $("phase-note").textContent = note;
  $("rec-dot").classList.toggle("on", answering);
  $("rec-label").textContent = answering ? "녹음 중" : "대기 중";
  renderTimers();
}

function showQuestionText(question, visible) {
  const node = $("question-text");
  node.textContent = visible ? question.text : "질문을 잘 듣고 답변하세요.";
  node.classList.toggle("concealed", !visible);
}

// 질문을 읽어 준다. 읽지 못하면 실전 모드여도 글로 보여준다.
async function deliverQuestion(question) {
  const spoken = await speak(question.text, question.interviewer);
  if (spoken) await sleep(AFTER_SPEECH_MS);
  else showQuestionText(question, true);
}

async function replayQuestion() {
  if (state.phase !== "answering" || state.replayUsed) return;
  state.replayUsed = true;
  // 다시 듣는 동안에는 녹음과 답변 타이머를 멈춘다. 전체 타이머는 계속 흐른다.
  const recorder = state.mode === "voice" ? state.recorder : null;
  if (recorder && recorder.state === "recording") recorder.pause();
  setPhase("asking");
  await deliverQuestion(state.question);
  if (state.phase !== "asking") return;
  setPhase("answering");
  if (recorder && recorder.state === "paused") recorder.resume();
}

async function presentQuestion(question) {
  state.question = question;
  $("question-count").textContent = `질문 ${question.index}`;
  showQuestionText(question, !(state.drill && tts.available));
  state.replayUsed = false;
  $("replay-button").hidden = !tts.available;
  for (const who of ["A", "B"]) {
    $(`badge-${who}`).classList.toggle("active", who === question.interviewer);
  }
  $("answer-text").value = "";
  state.answerMs = 0;

  setPhase("asking");
  await deliverQuestion(question);
  if (state.phase !== "asking") return;
  setPhase("answering");
  if (state.mode === "voice") state.recorder = startRecording(state.stream);
  else $("answer-text").focus();
}

function answerBudgetMs() {
  // 전체 시간이 먼저 끝나면 답변도 거기서 끊긴다.
  const totalLeft = state.totalLimitMs - state.activeMs;
  return Math.min(state.answerLimitMs - state.answerMs, totalLeft);
}

function tick() {
  const now = performance.now();
  const delta = now - state.lastTick;
  state.lastTick = now;
  if (state.phase === "asking" || state.phase === "answering") state.activeMs += delta;
  if (state.phase === "answering") {
    state.answerMs += delta;
    if (answerBudgetMs() <= 0) {
      submitAnswer();
      return;
    }
  }
  renderTimers();
}

function renderTimers() {
  const totalLeft = state.totalLimitMs - state.activeMs;
  $("total-timer").textContent = clock(totalLeft);
  $("total-timer").classList.toggle("low", totalLeft <= 60000);

  const answerLeft = Math.max(0, state.answerLimitMs - state.answerMs);
  const timer = $("answer-timer");
  timer.textContent = clock(answerLeft);
  timer.classList.toggle("low", state.phase === "answering" && answerLeft <= 10000);
  const meter = $("answer-meter");
  meter.style.width = `${Math.min(100, (state.answerMs / state.answerLimitMs) * 100)}%`;
  meter.classList.toggle("low", state.phase === "answering" && answerLeft <= 10000);
  if (state.mode === "voice") {
    const level = state.phase === "answering" ? micLevel() : 0;
    $("level-fill").style.width = `${level * 100}%`;
  }
}

async function submitAnswer() {
  if (state.phase !== "answering") return;
  const form = new FormData();
  form.set("answer_sec", (state.answerMs / 1000).toFixed(1));
  form.set("elapsed_sec", (state.activeMs / 1000).toFixed(1));
  setPhase("processing");
  if (state.mode === "voice") form.set("audio", await stopRecording(state.recorder), "answer.webm");
  else form.set("text", $("answer-text").value);
  state.pending = form;
  sendPending();
}

async function sendPending() {
  $("interview-error").hidden = true;
  let res;
  try {
    res = await api(`/api/sessions/${state.sessionId}/answers`, { method: "POST", body: state.pending });
  } catch (err) {
    $("interview-error-text").textContent = err.message;
    $("interview-error").hidden = false;
    return;
  }
  state.pending = null;
  if (res.done) finishInterview();
  else presentQuestion(res.question);
}

// ---------- 종료·리포트 화면 ----------

async function finishInterview() {
  clearInterval(state.tickId);
  closeMic();
  speak(CLOSING_REMARK, "A");
  state.phase = "done";
  state.review = null;
  showScreen("done");
  $("review").replaceChildren();
  $("report").hidden = true;
  $("report-error").hidden = true;
  $("done-summary").textContent = "";
  showError("done-error", "");
  try {
    state.review = await api(`/api/sessions/${state.sessionId}`);
    renderReview(state.review, null);
  } catch (err) {
    showError("done-error", err.message);
  }
  requestReport();
}

async function requestReport() {
  const status = $("report-status");
  $("report-error").hidden = true;
  status.hidden = false;
  status.textContent = "리포트를 작성하는 중입니다. 30초쯤 걸립니다.";
  let report;
  try {
    ({ report } = await api(`/api/sessions/${state.sessionId}/report`, { method: "POST" }));
  } catch (err) {
    $("report-error-text").textContent = err.message;
    $("report-error").hidden = false;
    return;
  } finally {
    status.hidden = true;
  }
  renderReport(report);
  if (state.review) renderReview(state.review, report);
}

function renderReport(report) {
  $("report-overall").textContent = report.overall;
  $("report-delivery").textContent = report.delivery;
  $("report-scores").replaceChildren(
    ...report.scores.map((score) => {
      const fill = el("div", { class: "meter-fill" });
      fill.style.width = `${(score.score_5 / 5) * 100}%`;
      return el(
        "div",
        { class: "score" },
        el(
          "div",
          { class: "score-head" },
          el("span", { class: "score-name", text: score.competency }),
          el("span", { class: "tag", text: `비중 ${Math.round(score.weight)}%` }),
          el("span", { class: "score-value", text: `${score.score_5.toFixed(1)} / 5` }),
        ),
        el("div", { class: "meter" }, fill),
        el("p", { class: "muted", text: score.reason }),
      );
    }),
  );
  $("report-next").replaceChildren(...report.next_practice.map((item) => el("li", { text: item })));
  $("report").hidden = false;
}

function feedbackRows(feedback) {
  const rows = [
    ["잘한 점", feedback.good, ""],
    ["보완할 점", feedback.improve, ""],
    ["다시 답한다면", feedback.direction, ""],
  ];
  if (feedback.mismatch) rows.push(["기록과 어긋난 부분", feedback.mismatch, "mismatch"]);
  return el(
    "dl",
    { class: "feedback" },
    ...rows.flatMap(([label, text, cls]) => [
      el("dt", { class: cls, text: label }),
      el("dd", { class: cls, text }),
    ]),
  );
}

function renderReview(session, report) {
  $("done-summary").textContent =
    `질문 ${session.turns.length}개 · 면접 시간 ${duration(session.elapsed_sec)}`;
  const feedbackByIndex = new Map((report ? report.per_question : []).map((q) => [q.index, q]));
  $("review").replaceChildren(
    ...session.turns.map((turn) => {
      const card = el(
        "article",
        { class: "review-card" },
        el(
          "div",
          { class: "review-meta" },
          el("span", { class: "badge active", text: `면접위원 ${turn.interviewer}` }),
          el("span", { class: "tag", text: turn.type === "followup" ? "꼬리질문" : "새 질문" }),
          el("span", { class: "tag", text: turn.competency }),
          el("span", { class: "muted small", text: `답변 ${duration(turn.answer_sec ?? 0)}` }),
        ),
        el("h3", { text: `${turn.index}. ${turn.text}` }),
        el("p", { class: "review-answer", text: turn.answer ?? "(답변 없음)" }),
      );
      if (turn.evidence) {
        card.append(
          el(
            "p",
            { class: "muted small" },
            el("strong", { text: "근거 기록 " }),
            turn.evidence + (turn.evidence_verified ? "" : " (생기부 원문에서 찾지 못함)"),
          ),
        );
      }
      const feedback = feedbackByIndex.get(turn.index);
      if (feedback) card.append(feedbackRows(feedback));
      return card;
    }),
  );
}

// ---------- 이벤트 ----------

$("start-button").addEventListener("click", startInterview);
$("mic-test-button").addEventListener("click", runMicTest);
$("mode-voice").addEventListener("change", () => setMode("voice"));
$("mode-text").addEventListener("change", () => setMode("text"));
$("submit-button").addEventListener("click", submitAnswer);
$("retry-button").addEventListener("click", sendPending);
$("report-retry-button").addEventListener("click", requestReport);
$("tts-test-button").addEventListener("click", runTtsTest);
$("replay-button").addEventListener("click", replayQuestion);
$("restart-button").addEventListener("click", () => {
  stopSpeaking();
  state.phase = "setup";
  showScreen("setup");
});
$("answer-text").addEventListener("keydown", (event) => {
  // 한글 조합 중의 Enter는 글자 확정이므로 제출하지 않는다.
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && !event.isComposing) {
    event.preventDefault();
    submitAnswer();
  }
});
window.addEventListener("beforeunload", (event) => {
  if (["asking", "answering", "processing"].includes(state.phase)) event.preventDefault();
});

if ("speechSynthesis" in window) speechSynthesis.addEventListener("voiceschanged", loadVoices);
loadVoices();
loadSetup();
