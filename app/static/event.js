// Visitor page: take a selfie or upload a photo, then search. Both ways produce the same
// kind of image file, so the search, timer and results are identical. The photo is sent
// once, when "Find my photos" is clicked, and removed from the page after the search.
// Desktop: the page shows the whole event as a gallery; a floating camera button opens the
// photo panel, and matches replace the gallery until "Back to all event photos" is clicked.
(() => {
  const root = document.getElementById("visitor");
  const form = document.getElementById("search-form");
  if (!root || !form) return; // search is turned off for this event

  const token = root.dataset.token;
  const maxMb = Number(root.dataset.maxMb);
  const MAX_SELFIE_SIDE = 1600; // plenty for face matching, keeps uploads small on phones
  const $ = (id) => document.getElementById(id);
  const input = $("selfie-input");
  const drop = $("selfie-drop");
  const preview = $("selfie-preview");
  const hint = $("selfie-hint");
  const hintDefault = hint.innerHTML;
  const consent = $("consent");
  const button = $("search-btn");
  const message = $("message");
  const results = $("results");
  const resultsTitle = $("results-title");
  const grid = $("result-grid");
  const viewer = $("viewer");
  const replaceBtn = $("upload-replace"); // desktop only
  const desktop = window.matchMedia("(min-width: 900px) and (hover: hover) and (pointer: fine)"); // same as style.css
  const searchCard = $("search-card");
  const galleryView = $("gallery");

  let file = null;          // what "Find my photos" sends
  let uploadedFile = null;  // each tab remembers its own choice
  let selfieFile = null;
  let searching = false;

  // Russian plural forms: plural(5, "фотография", "фотографии", "фотографий") → "фотографий"
  const plural = (n, one, few, many) => {
    const m10 = n % 10, m100 = n % 100;
    return m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? few : many;
  };
  const photos = (n) => `${n} ${plural(n, "фотография", "фотографии", "фотографий")}`;

  function showMessage(kind, text) {
    message.innerHTML = "";
    if (!text) return;
    const p = document.createElement("p");
    p.className = `alert alert-${kind}`;
    p.textContent = text;
    message.appendChild(p);
  }

  function updateButton() {
    button.disabled = searching || !(file && consent.checked);
  }

  // ---- upload an existing photo
  // Large photos are reduced in the browser to the same size as camera selfies (longest side
  // 1600 px) before they are sent: plenty for face matching, much faster, and small enough for
  // any hosting limit on request size. Files the browser cannot decode (e.g. HEIC outside
  // Safari) are sent unchanged and the server reads them.
  const SHRINK_ABOVE_BYTES = 2.5 * 1024 * 1024;
  async function shrink(f) {
    if (!("createImageBitmap" in window)) return f;
    let bitmap;
    try { bitmap = await createImageBitmap(f, { imageOrientation: "from-image" }); } catch { return f; }
    const scale = Math.min(1, MAX_SELFIE_SIDE / Math.max(bitmap.width, bitmap.height));
    if (scale === 1 && f.size <= SHRINK_ABOVE_BYTES) { bitmap.close(); return f; }
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bitmap.width * scale);
    canvas.height = Math.round(bitmap.height * scale);
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#fff"; // transparent PNG areas become white, not black
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    bitmap.close();
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.92));
    return blob ? new File([blob], "photo.jpg", { type: "image/jpeg" }) : f;
  }

  let pickRequest = 0; // a newer choice wins if two photos are picked quickly
  async function pick(chosen) {
    showMessage();
    if (!chosen) return;
    const request = ++pickRequest;
    if (mode !== "upload") setMode("upload");
    let f = chosen;
    const isImage = f.type.startsWith("image/") || /\.(heic|heif)$/i.test(f.name);
    if (!isImage) {
      showMessage("error", "Выберите файл изображения (JPG, PNG, WEBP или HEIC).");
      return;
    }
    if (f.size > maxMb * 1024 * 1024) {
      showMessage("error", `Эта фотография больше ${maxMb} МБ. Выберите файл поменьше.`);
      return;
    }
    f = await shrink(f);
    if (request !== pickRequest) return;
    file = uploadedFile = f;
    if (preview.src) URL.revokeObjectURL(preview.src);
    preview.src = URL.createObjectURL(f);
    preview.hidden = false;
    hint.innerHTML = '<span class="muted small">Нажмите, чтобы выбрать другую фотографию</span>';
    replaceBtn.hidden = false;
    updateButton();
    if (desktop.matches) { openComposer(); consent.focus(); }
  }

  function clearUpload() {
    uploadedFile = null;
    if (preview.src) URL.revokeObjectURL(preview.src);
    preview.removeAttribute("src");
    preview.hidden = true;
    hint.innerHTML = hintDefault;
    replaceBtn.hidden = true;
    input.value = "";
  }

  preview.addEventListener("error", () => (preview.hidden = true)); // e.g. HEIC can't preview in some browsers
  input.addEventListener("change", () => pick(input.files[0]));
  consent.addEventListener("change", updateButton);
  ["dragenter", "dragover"].forEach((t) =>
    drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("dragging"); }));
  ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, () => drop.classList.remove("dragging")));
  drop.addEventListener("drop", (e) => { e.preventDefault(); pick(e.dataTransfer.files[0]); });
  replaceBtn.addEventListener("click", () => input.click());

  // ---- tabs: "Take a photo" / "Upload a photo" (keyboard: arrow keys, Home, End)
  const tabs = { camera: $("tab-camera"), upload: $("tab-upload") };
  const order = ["camera", "upload"];
  const panels = { camera: $("camera-panel"), upload: $("upload-panel") };
  let mode = "upload";

  function setMode(next, focus = false) {
    mode = next;
    for (const name of order) {
      const selected = name === next;
      tabs[name].setAttribute("aria-selected", String(selected));
      tabs[name].tabIndex = selected ? 0 : -1;
      panels[name].hidden = !selected;
    }
    if (focus) tabs[next].focus();
    if (next !== "camera") stopCamera(); // never leave the camera on in the background
    file = next === "camera" ? selfieFile : uploadedFile;
    showMessage();
    updateButton();
  }
  for (const name of order) {
    tabs[name].addEventListener("click", () => setMode(name));
    tabs[name].addEventListener("keydown", (e) => {
      const i = order.indexOf(name);
      const target = { ArrowRight: order[(i + 1) % order.length], ArrowLeft: order[(i + order.length - 1) % order.length],
                       Home: order[0], End: order[order.length - 1] }[e.key];
      if (target) { e.preventDefault(); setMode(target, true); }
    });
  }

  // ---- camera: live preview -> take photo -> review -> retake
  const video = $("camera-video");
  const shot = $("camera-shot");
  const placeholder = $("camera-placeholder");
  const startBtn = $("camera-start");
  const snapBtn = $("camera-snap");
  const retakeBtn = $("camera-retake");
  const cameraError = $("camera-error");
  let stream = null;
  let cameraRequest = 0; // lets a late answer to an old camera request be ignored

  function showCameraError(text) {
    cameraError.textContent = text;
    cameraError.hidden = !text;
  }

  function cameraErrorText(err) {
    switch (err && err.name) {
      case "NotAllowedError":
      case "SecurityError":
        return "Доступ к камере запрещён. Чтобы воспользоваться камерой, разрешите доступ к ней для этого сайта " +
               "в настройках браузера (на компьютере — значок камеры в адресной строке, на телефоне — настройки сайта) " +
               "и в настройках конфиденциальности устройства, затем снова нажмите «Включить камеру». " +
               "Можно также выбрать «Загрузить фото».";
      case "NotFoundError":
      case "OverconstrainedError":
        return "На этом устройстве не найдена камера. Воспользуйтесь кнопкой «Загрузить фото».";
      case "NotSupportedError":
        return "Этот браузер не может использовать камеру на этой странице. Воспользуйтесь кнопкой «Загрузить фото».";
      case "NotReadableError":
      case "AbortError":
        return "Камера занята другим приложением или не смогла включиться. Закройте другие приложения, " +
               "которые используют камеру, и попробуйте снова — или выберите «Загрузить фото».";
      default:
        return "Не удалось включить камеру. Попробуйте ещё раз или выберите «Загрузить фото».";
    }
  }

  function resetStartButton() {
    startBtn.disabled = false;
    startBtn.textContent = selfieFile ? "Сделать другое фото" : "Включить камеру";
  }

  async function startCamera() {
    showCameraError("");
    showMessage();
    if (!window.isSecureContext) {
      showCameraError("Камера работает только на защищённой странице (https://). Выберите «Загрузить фото» " +
                      "или попросите у организатора ссылку с https.");
      return;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      showCameraError("Этот браузер не даёт доступа к камере. Воспользуйтесь кнопкой «Загрузить фото».");
      return;
    }
    const request = ++cameraRequest;
    startBtn.disabled = true;
    startBtn.textContent = "Ждём разрешения…";
    let newStream;
    try {
      newStream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: "user", width: { ideal: 1280 }, height: { ideal: 960 } },
        audio: false,
      });
    } catch (err) {
      if (request === cameraRequest) {
        showCameraError(cameraErrorText(err));
        resetStartButton();
      }
      return;
    }
    // The visitor may have switched tabs or left the page while the permission prompt was open.
    if (request !== cameraRequest || mode !== "camera" || document.hidden) {
      newStream.getTracks().forEach((t) => t.stop());
      resetStartButton();
      return;
    }
    stream = newStream;
    video.srcObject = stream;
    await video.play().catch(() => {});
    if (request !== cameraRequest || mode !== "camera" || document.hidden) {
      newStream.getTracks().forEach((t) => t.stop()); // left while the video was starting
      return;
    }
    selfieFile = null;
    file = null;
    updateButton();
    placeholder.hidden = true;
    shot.hidden = true;
    video.hidden = false;
    startBtn.hidden = true;
    resetStartButton();
    retakeBtn.hidden = true;
    snapBtn.hidden = false;
    snapBtn.focus();
  }

  function stopCamera() {
    cameraRequest++; // cancels a request that is still waiting for permission
    if (stream) stream.getTracks().forEach((track) => track.stop()); // turns the camera light off
    stream = null;
    video.srcObject = null;
    video.hidden = true;
    snapBtn.hidden = true;
    if (!selfieFile) {
      placeholder.hidden = false;
      startBtn.hidden = false;
      resetStartButton();
    }
  }

  function takePhoto() {
    if (!video.videoWidth) return; // camera not ready yet
    const scale = Math.min(1, MAX_SELFIE_SIDE / Math.max(video.videoWidth, video.videoHeight));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(video.videoWidth * scale);
    canvas.height = Math.round(video.videoHeight * scale);
    canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
    canvas.toBlob((blob) => {
      if (!blob) { showCameraError("Не удалось сделать снимок. Попробуйте ещё раз."); return; }
      // Kept only in this page's memory; sent once, when "Find my photos" is clicked.
      selfieFile = new File([blob], "selfie.jpg", { type: "image/jpeg" });
      if (shot.src) URL.revokeObjectURL(shot.src);
      shot.src = URL.createObjectURL(selfieFile);
      shot.hidden = false;
      stopCamera();
      retakeBtn.hidden = false;
      file = selfieFile;
      updateButton();
      showMessage("info", consent.checked
        ? "Проверьте селфи. Если всё хорошо, нажмите «Найти мои фото» или «Переснять»."
        : "Проверьте селфи, отметьте согласие и нажмите «Найти мои фото» (или «Переснять»).");
    }, "image/jpeg", 0.92);
  }

  function clearSelfie() {
    selfieFile = null;
    if (shot.src) URL.revokeObjectURL(shot.src);
    shot.removeAttribute("src");
    shot.hidden = true;
    retakeBtn.hidden = true;
    placeholder.hidden = false;
    startBtn.hidden = false;
    resetStartButton();
  }

  function retake() {
    clearSelfie();
    file = null;
    updateButton();
    startCamera();
  }

  startBtn.addEventListener("click", startCamera);
  snapBtn.addEventListener("click", takePhoto);
  retakeBtn.addEventListener("click", retake);
  window.addEventListener("pagehide", stopCamera);
  document.addEventListener("visibilitychange", () => { if (document.hidden) stopCamera(); });
  setMode("upload"); // initial tab; must run after the camera variables above exist

  // ---- desktop: floating camera button (bottom-right) -> "Take photo" / "Upload photo".
  // The chosen photo, consent and "Find my photos" then appear in a small panel (the same
  // form phones see on the page), so camera, upload and search code are shared.
  const fab = $("photo-fab");
  const menu = $("photo-menu");
  const menuItems = [$("menu-camera"), $("menu-upload")];

  function openMenu() {
    menu.hidden = false;
    fab.setAttribute("aria-expanded", "true");
    menuItems[0].focus();
  }
  function closeMenu(focusFab = false) {
    if (menu.hidden) return;
    menu.hidden = true;
    fab.setAttribute("aria-expanded", "false");
    if (focusFab) fab.focus();
  }
  function openComposer() {
    searchCard.classList.add("open");
  }
  function closeComposer() {
    if (!searchCard.classList.contains("open")) return;
    searchCard.classList.remove("open");
    stopCamera(); // never leave the camera on behind a closed panel
  }

  fab.addEventListener("click", () => (menu.hidden ? openMenu() : closeMenu()));
  fab.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenu(true); });
  $("menu-camera").addEventListener("click", () => {
    closeMenu();
    if (mode !== "camera") setMode("camera");
    openComposer();
    if (stream) snapBtn.focus(); // the camera is already on
    else startCamera();          // straight to the live preview, no extra "Turn on camera" click
  });
  $("menu-upload").addEventListener("click", () => {
    closeMenu();
    closeComposer(); // also turns a running camera off
    input.click();   // the computer's file chooser; the panel opens once a photo is chosen
  });
  $("composer-close").addEventListener("click", () => { closeComposer(); fab.focus(); });
  menu.addEventListener("keydown", (e) => {
    const i = menuItems.indexOf(document.activeElement);
    if (e.key === "Escape") { e.preventDefault(); closeMenu(true); }
    else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      menuItems[(i + (e.key === "ArrowDown" ? 1 : menuItems.length - 1)) % menuItems.length].focus();
    } else if (e.key === "Home" || e.key === "End") {
      e.preventDefault();
      menuItems[e.key === "Home" ? 0 : menuItems.length - 1].focus();
    } else if (e.key === "Tab") closeMenu();
  });
  document.addEventListener("click", (e) => { if (!$("photo-fab-wrap").contains(e.target)) closeMenu(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && menu.hidden && !viewer.open && !document.body.classList.contains("menu-open") &&
        searchCard.classList.contains("open")) {
      closeComposer();
      fab.focus();
    }
  });

  // ---- search
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!file || !consent.checked || searching) return;
    searching = true;
    const sentFrom = mode;
    const sent = file;
    updateButton();
    button.textContent = "Ищем…";
    results.hidden = true;
    showMessage("info", "Ищем ваше лицо на фотографиях мероприятия…");
    startTimer();
    let data = null;
    let ok = false;
    try {
      // Sent as the raw request body; the server keeps it in memory for this search only.
      // If the server is busy, try again automatically a few times (the timer keeps running).
      let res;
      for (let attempt = 0; ; attempt++) {
        res = await fetch(`/e/${token}/search`, {
          method: "POST",
          body: sent,
          headers: { "Content-Type": sent.type || "application/octet-stream" },
        });
        data = await res.json().catch(() => ({ message: "Что-то пошло не так. Попробуйте ещё раз." }));
        if (res.status !== 503 || data.error !== "busy" || attempt >= 3) break;
        showMessage("info", "Сейчас ищут многие — повторим попытку через мгновение…");
        await new Promise((resolve) => setTimeout(resolve, 1500 * (attempt + 1)));
      }
      if (!res.ok) {
        const retakeTip = sentFrom === "camera" && ["no_face", "multiple_faces", "face_too_small"].includes(data.error)
          ? " Сделайте новое селфи и попробуйте снова." : "";
        showMessage("error", data.message + retakeTip);
      } else {
        ok = true;
        await showResults(data);
      }
    } catch {
      showMessage("error", "Не удалось связаться с сервером. Проверьте подключение к интернету и попробуйте снова.");
    } finally {
      stopTimer(ok ? data : null);
      // Remove the photo that was sent from the page: it was used for this one search only.
      // (A new photo chosen while the search was running is kept.)
      if (sentFrom === "camera" && selfieFile === sent) clearSelfie();
      if (sentFrom === "upload" && uploadedFile === sent) clearUpload();
      if (file === sent) file = null;
      searching = false;
      button.textContent = "Найти мои фото";
      updateButton();
    }
  });

  // ---- visible search timer: starts on click, stops when the results are on screen
  let timerStart = 0;
  let timerTick = null;
  const timerBox = $("search-timer");
  const timerClock = $("timer-clock");
  const timerLabel = $("timer-label");
  const timerDetail = $("timer-detail");
  const seconds = (ms) => `${(ms / 1000).toFixed(2).replace(".", ",")} с`;

  function startTimer() {
    timerStart = performance.now();
    timerBox.hidden = false;
    timerBox.classList.add("running");
    timerLabel.textContent = "Ищем…";
    timerDetail.textContent = "";
    timerClock.textContent = seconds(0);
    timerTick = setInterval(() => (timerClock.textContent = seconds(performance.now() - timerStart)), 50);
  }

  function stopTimer(data) {
    clearInterval(timerTick);
    const total = performance.now() - timerStart;
    timerBox.classList.remove("running");
    timerClock.textContent = seconds(total);
    timerLabel.textContent = "Время поиска";
    if (!data || !data.timing) return;
    $("results-time").textContent = `Время поиска ${seconds(total)}`;
    const t = data.timing;
    const rest = Math.max(0, total - t.server_ms);
    timerDetail.textContent =
      `Получение фото ${seconds(t.receive_ms)} · поиск лица ${seconds(t.face_ms)} · ` +
      `сравнение с ${data.compared_faces} ${plural(data.compared_faces, "лицом", "лицами", "лицами")} ` +
      `на ${data.searched_photos} ${plural(data.searched_photos, "фотографии", "фотографиях", "фотографиях")} ${seconds(t.match_ms)} · ` +
      `сеть и показ результатов ${seconds(rest)}.`;
    $("results-timing").textContent = timerDetail.textContent;
  }

  function firstThumbnailLoaded() {
    const img = grid.querySelector("img");
    if (!img || img.complete) return Promise.resolve();
    return new Promise((resolve) => {
      img.addEventListener("load", resolve, { once: true });
      img.addEventListener("error", resolve, { once: true });
      setTimeout(resolve, 3000); // never let a slow image hold the timer
    });
  }

  async function showResults(data) {
    const waiting = data.waiting_photos > 0
      ? ` Ещё ${photos(data.waiting_photos)} в обработке — повторите поиск позже, чтобы учесть и их.`
      : "";
    const removed = " Ваше фото использовалось только для этого поиска и удалено со страницы.";
    if (data.matches.length === 0) {
      showMessage("empty", `Среди ${data.searched_photos} ${plural(data.searched_photos, "фотографии", "фотографий", "фотографий")} мероприятия возможных совпадений не найдено. ` +
        "Попробуйте другое своё фото: чёткое, при хорошем освещении, лицом к камере." + waiting + removed);
      return;
    }
    showMessage("success", "Поиск завершён." + removed + waiting);
    const n = data.matches.length;
    resultsTitle.textContent = `${n} ${plural(n, "возможное совпадение", "возможных совпадения", "возможных совпадений")}`;
    $("results-timing").textContent = "";
    $("results-time").textContent = "";
    grid.innerHTML = "";
    matchList = data.matches;
    data.matches.forEach((m, i) => grid.appendChild(resultTile(m, i + 1)));
    results.hidden = false;
    if (desktop.matches) {
      // Separate results view: the gallery steps aside until "Back to all event photos".
      closeComposer();
      galleryView.hidden = true;
      sphere.pause();
      window.scrollTo({ top: 0 });
      resultsTitle.focus({ preventScroll: true });
    } else {
      results.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    await firstThumbnailLoaded();
  }

  let matchList = [];

  function resultTile(match, number) {
    const tile = document.createElement("figure");
    tile.className = "tile";

    const open = document.createElement("button");
    open.type = "button";
    open.className = "tile-img";
    open.setAttribute("aria-label", `Открыть возможное совпадение ${number}`);
    const img = document.createElement("img");
    img.src = match.thumb;
    img.alt = `Возможное совпадение ${number}`;
    img.loading = number <= 8 ? "eager" : "lazy";
    open.appendChild(img);
    open.addEventListener("click", () => openViewer(matchList, number - 1, true, open));

    const body = document.createElement("figcaption");
    body.className = "tile-body";
    const badge = document.createElement("span");
    badge.innerHTML = '<span class="badge badge-warn">Возможное совпадение</span>';
    const note = document.createElement("span");
    note.className = "muted";
    note.textContent = match.strength === "higher" ? "Сильное сходство" : "Слабое сходство — проверьте";

    const actions = document.createElement("div");
    actions.className = "tile-actions";
    const view = document.createElement("button");
    view.type = "button";
    view.className = "btn btn-ghost btn-small";
    view.textContent = "Открыть";
    view.addEventListener("click", () => openViewer(matchList, number - 1, true, open));
    const dl = document.createElement("a");
    dl.className = "btn btn-small";
    dl.href = match.download;
    dl.textContent = "Скачать";
    dl.setAttribute("download", "");
    actions.append(view, dl);

    body.append(badge, note, actions);
    tile.append(open, body);
    return tile;
  }

  // ---- motion: on for desktop unless the visitor asked their system for reduced motion
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const animate = () => desktop.matches && !reducedMotion.matches;
  const body = document.body;
  if (animate()) body.classList.add("motion");

  // The event name rises in word by word (the delays are set here: inline styles are not allowed).
  const eventTitle = $("event-title");
  if (eventTitle) {
    const words = eventTitle.textContent.trim().split(/\s+/);
    eventTitle.textContent = "";
    words.forEach((word, i) => {
      const span = document.createElement("span");
      span.className = "word";
      span.textContent = word;
      span.style.setProperty("--i", String(i));
      eventTitle.append(span, i < words.length - 1 ? " " : "");
    });
  }

  // Splash: a short title card while the first thumbnails arrive; never longer than ~2 s.
  const splash = $("splash");
  const splashBar = $("splash-bar");
  const splashBorn = performance.now();
  let revealed = false;
  function reveal() {
    if (revealed) return;
    revealed = true;
    if (splashBar) splashBar.style.transform = "scaleX(1)";
    const finish = () => {
      body.classList.add("revealed");
      if (!splash) return;
      if (!animate()) { splash.remove(); return; }   // reduced motion or phone: no fade at all
      splash.classList.add("out");
      setTimeout(() => splash.remove(), 700);
    };
    if (animate() && splash) setTimeout(finish, 220);
    else finish();
  }
  function splashProgress(fraction) {
    if (splashBar && !revealed) splashBar.style.transform = `scaleX(${Math.min(1, fraction)})`;
  }
  if (!animate()) reveal();
  else setTimeout(reveal, 1400); // backstop: the archive is never held back longer

  // ---- large view: FLIP from the clicked thumbnail, thumbnail first, sharp preview when it has loaded
  const viewerImg = $("viewer-img");
  const viewerPlate = $("viewer-plate");
  const viewerPrev = $("viewer-prev");
  const viewerNext = $("viewer-next");
  let viewerList = [];
  let viewerIndex = 0;
  let viewerIsMatch = false;
  let viewerSource = null;   // element the photo grew out of, for the way back
  let viewerToken = 0;
  let viewerClosing = false;

  function sourceFor(index) {
    if (viewerIsMatch) return grid.querySelectorAll(".tile-img")[index] || null;
    if (!galleryView.hidden && !gridBox.hidden) return galleryGrid.children[index] || null;
    return null;
  }

  function showInViewer(index) {
    viewerIndex = (index + viewerList.length) % viewerList.length;
    const photo = viewerList[viewerIndex];
    const token = ++viewerToken;
    viewerImg.src = photo.thumb || photo.view;      // already in the browser: shows at once
    const sharp = new Image();
    sharp.onload = () => { if (token === viewerToken) viewerImg.src = photo.view; };
    sharp.src = photo.view;
    const n = viewerIndex + 1;
    $("viewer-title").textContent = viewerIsMatch ? `Возможное совпадение ${n}` : `№ ${String(n).padStart(3, "0")}`;
    $("viewer-count").textContent = `${n} из ${viewerList.length}`;
    viewerImg.alt = viewerIsMatch ? `Возможное совпадение ${n} из ${viewerList.length}` : `Фото мероприятия ${n} из ${viewerList.length}`;
    $("viewer-note").textContent = viewerIsMatch
      ? (photo.strength === "higher" ? "Сильное сходство." : "Слабое сходство — проверьте, вы ли это.") +
        " Автоматическое распознавание лиц может ошибаться."
      : "«Скачать» сохраняет чистую копию фотографии — без данных о месте съёмки и камере.";
    $("viewer-download").href = photo.download;
    for (const step of [1, -1]) { // have the neighbours ready, so stepping through feels instant
      const next = viewerList[(viewerIndex + step + viewerList.length) % viewerList.length];
      if (next) new Image().src = next.view;
    }
  }

  function flip(fromEl, reverse) {
    // Moves the plate between the thumbnail's rectangle and its own place (First-Last-Invert-Play).
    if (!animate() || !fromEl) return false;
    const from = fromEl.getBoundingClientRect();
    if (from.width === 0 || from.bottom < 0 || from.top > innerHeight) return false;
    const to = viewerPlate.getBoundingClientRect();
    const scale = Math.max(0.04, from.width / to.width);
    const invert = `translate(${from.left - to.left}px, ${from.top - to.top}px) scale(${scale})`;
    if (!reverse) {
      viewerPlate.classList.add("flipping");
      viewerPlate.style.transform = invert;
      viewerPlate.style.opacity = "0";
      void viewerPlate.offsetWidth; // apply the start position before animating home
      viewerPlate.classList.remove("flipping");
      viewerPlate.style.transform = "";
      viewerPlate.style.opacity = "";
    } else {
      viewerPlate.style.transform = invert;
      viewerPlate.style.opacity = "0";
    }
    return true;
  }

  function openViewer(list, index, isMatch, sourceEl = null) {
    viewerList = list;
    viewerIsMatch = isMatch;
    viewerSource = sourceEl;
    viewerClosing = false;
    viewerPrev.hidden = viewerNext.hidden = list.length < 2;
    $("viewer-badge").hidden = !isMatch;
    showInViewer(index);
    if (!viewer.open) {
      viewer.showModal();
      flip(sourceEl, false);
      sphere.pause();
    }
  }

  function closeViewer() {
    if (!viewer.open || viewerClosing) return;
    viewerClosing = true;
    const back = sourceFor(viewerIndex) || viewerSource;
    const done = () => {
      viewer.close();
      viewerPlate.classList.add("flipping");
      viewerPlate.style.transform = "";
      viewerPlate.style.opacity = "";
      void viewerPlate.offsetWidth;
      viewerPlate.classList.remove("flipping");
      viewerClosing = false;
      if (back && document.contains(back)) back.focus({ preventScroll: true });
      sphere.resume();
    };
    if (flip(back, true)) setTimeout(done, 430);
    else done();
  }

  viewerPrev.addEventListener("click", () => showInViewer(viewerIndex - 1));
  viewerNext.addEventListener("click", () => showInViewer(viewerIndex + 1));
  viewer.addEventListener("keydown", (e) => {
    if (viewerList.length < 2) return;
    if (e.key === "ArrowLeft") { e.preventDefault(); showInViewer(viewerIndex - 1); }
    if (e.key === "ArrowRight") { e.preventDefault(); showInViewer(viewerIndex + 1); }
  });
  viewer.addEventListener("cancel", (e) => { e.preventDefault(); closeViewer(); }); // Escape
  $("viewer-close").addEventListener("click", closeViewer);
  viewer.addEventListener("click", (e) => { if (e.target === viewer || e.target === $("viewer-lit")) closeViewer(); });

  $("results-back").addEventListener("click", () => {
    results.hidden = true;
    galleryView.hidden = false;
    window.scrollTo({ top: 0 });
    $("gallery-title").focus({ preventScroll: true });
    if (currentView === "sphere") sphere.resume();
  });

  // ---- desktop gallery: every finished photo of this event, all shown at once as a grid
  // (thumbnails load as they scroll into view). Phones keep the search form only, for now.
  const galleryGrid = $("gallery-grid");
  const gridBox = galleryGrid;
  const galleryStatus = $("gallery-status");
  const galleryCount = $("gallery-count");
  const galleryPhotos = [];
  let galleryStarted = false;
  let firstThumbs = 0;
  const FIRST_THUMBS = 12; // the splash waits (briefly) for about the first screenful

  function galleryTile(photo, index) {
    const open = document.createElement("button");
    open.type = "button";
    open.className = "library-tile";
    open.tabIndex = index === 0 ? 0 : -1;           // one tab stop; arrow keys move inside the grid
    open.setAttribute("aria-label", `Открыть фото ${index + 1}`);
    const img = document.createElement("img");
    img.src = photo.thumb;
    img.alt = "";
    img.loading = index < 40 ? "eager" : "lazy";
    img.decoding = "async";
    img.addEventListener("load", () => {
      open.classList.add("loaded");
      if (index < FIRST_THUMBS) splashProgress(0.25 + 0.75 * (++firstThumbs / FIRST_THUMBS));
      if (firstThumbs >= Math.min(FIRST_THUMBS, galleryPhotos.length)) {
        setTimeout(reveal, Math.max(0, 350 - (performance.now() - splashBorn))); // at least a short moment
      }
    }, { once: true });
    const cap = document.createElement("span");
    cap.className = "cap";
    cap.textContent = `№ ${String(index + 1).padStart(3, "0")}`;
    open.append(img, cap);
    if (index < 24) { open.classList.add("rise"); open.style.setProperty("--k", String(index)); }
    open.addEventListener("click", () => openViewer(galleryPhotos, index, false, open));
    return open;
  }

  function gridColumns() {
    return getComputedStyle(galleryGrid).gridTemplateColumns.split(" ").filter(Boolean).length || 1;
  }
  galleryGrid.addEventListener("keydown", (e) => {
    const tiles = galleryGrid.children;
    const i = Array.prototype.indexOf.call(tiles, document.activeElement);
    if (i < 0) return;
    const cols = gridColumns();
    const pageRows = Math.max(1, Math.floor(innerHeight / (tiles[0].offsetHeight || 1)) - 1);
    const target = { ArrowRight: i + 1, ArrowLeft: i - 1, ArrowDown: i + cols, ArrowUp: i - cols,
                     Home: 0, End: tiles.length - 1, PageDown: i + cols * pageRows, PageUp: i - cols * pageRows }[e.key];
    if (target === undefined) return;
    e.preventDefault();
    const next = tiles[Math.max(0, Math.min(tiles.length - 1, target))];
    tiles[i].tabIndex = -1;
    next.tabIndex = 0;
    next.focus({ preventScroll: true });
    next.scrollIntoView({ block: "nearest", behavior: animate() ? "smooth" : "auto" });
  });

  async function loadGallery() {
    galleryStatus.textContent = "Загружаем фотографии мероприятия…";
    let after = 0;
    let waiting = 0;
    try {
      while (after !== null) {
        const res = await fetch(`/e/${token}/gallery?after=${after}`);
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.message || "Не удалось загрузить фотографии.");
        if (typeof data.total === "number") {
          galleryCount.textContent = `${data.total.toLocaleString("ru")} ${plural(data.total, "фотография", "фотографии", "фотографий")}`;
          galleryCount.hidden = false;
          waiting = data.waiting || 0;
          splashProgress(0.25);
        }
        const tiles = document.createDocumentFragment();
        for (const photo of data.photos) tiles.appendChild(galleryTile(photo, galleryPhotos.push(photo) - 1));
        galleryGrid.appendChild(tiles);
        after = data.next;
      }
      if (galleryPhotos.length === 0) reveal();
      galleryStatus.textContent = galleryPhotos.length === 0
        ? (waiting ? "Фотографии ещё готовятся. Обновите страницу через несколько минут."
                   : "В это мероприятие пока не добавлено ни одной фотографии.")
        : (waiting ? `Ещё ${photos(waiting)} в обработке — они появятся после обновления страницы.` : "");
      if (currentView === "sphere") sphere.build();
    } catch (err) {
      galleryStatus.textContent = `${err.message || "Не удалось загрузить фотографии."} Обновите страницу.`;
      reveal();
    }
  }

  // Thumbnail size; remembered in this browser only.
  const ZOOM = [150, 230, 320, 440];
  let zoom = 1;
  try { zoom = Math.min(ZOOM.length - 1, Math.max(0, Number(localStorage.getItem("fmp-zoom2") ?? 1))); } catch { /* default */ }
  function applyZoom() {
    galleryGrid.style.setProperty("--tile", `${ZOOM[zoom]}px`);
    $("zoom-out").disabled = zoom === 0;
    $("zoom-in").disabled = zoom === ZOOM.length - 1;
    try { localStorage.setItem("fmp-zoom2", String(zoom)); } catch { /* private window */ }
  }
  $("zoom-out").addEventListener("click", () => { zoom = Math.max(0, zoom - 1); applyZoom(); });
  $("zoom-in").addEventListener("click", () => { zoom = Math.min(ZOOM.length - 1, zoom + 1); applyZoom(); });
  applyZoom();

  // ---- grid / sphere
  let currentView = "grid";
  function setView(next) {
    currentView = next;
    const isSphere = next === "sphere";
    $("view-grid").setAttribute("aria-pressed", String(!isSphere));
    $("view-sphere").setAttribute("aria-pressed", String(isSphere));
    galleryGrid.hidden = isSphere;
    $("zoom").hidden = isSphere;
    $("sphere").hidden = !isSphere;
    if (isSphere) {
      sphere.build();
      sphere.resume();
      // bring the whole sphere into view, just under the sticky bar
      $("sphere").scrollIntoView({ block: "end", behavior: animate() ? "smooth" : "auto" });
    } else sphere.pause();
  }
  $("view-grid").addEventListener("click", () => setView("grid"));
  $("view-sphere").addEventListener("click", () => setView("sphere"));

  // A sphere of highlights (evenly spaced through the event) on a Fibonacci lattice. The event name
  // sits at its centre and counter-rotates so it always faces you. Drag, or arrow keys, to turn it.
  const sphere = (() => {
    const stage = $("stage");
    const world = $("world");
    const orb = $("orb");
    const title = $("sphere-title");
    const COUNT = 36;
    const DEG = 180 / Math.PI;
    let cards = [];          // {el, x, y, z, index, d, o}
    let R = 300;
    let built = false;
    let running = false;
    let frame = 0;
    let spin = 0, tilt = -4, dragX = 0, dragY = 0, velX = 0, velY = 0;
    const PITCH = 32;
    let front = null;

    function build() {
      if (built || galleryPhotos.length === 0) return;
      built = true;
      const n = Math.min(COUNT, galleryPhotos.length);
      const GA = Math.PI * (3 - Math.sqrt(5));
      for (let i = 0; i < n; i++) {
        const index = n === 1 ? 0 : Math.round(i * (galleryPhotos.length - 1) / (n - 1));
        const y = n === 1 ? 0 : 1 - (i / (n - 1)) * 2;
        const rad = Math.sqrt(Math.max(0, 1 - y * y));
        const theta = i * GA;
        const el = document.createElement("div");
        el.className = "card3d";
        const fig = document.createElement("figure");
        const img = document.createElement("img");
        img.alt = "";
        img.decoding = "async";
        img.addEventListener("load", () => el.classList.add("in"), { once: true });
        img.src = galleryPhotos[index].thumb;
        fig.appendChild(img);
        el.appendChild(fig);
        orb.appendChild(el);
        cards.push({ el, x: Math.cos(theta) * rad, y, z: Math.sin(theta) * rad, index, d: -1, o: -1 });
      }
      layout();
    }

    function layout() {
      const w = stage.clientWidth, h = stage.clientHeight;
      if (!w || !h) return;
      R = Math.max(155, Math.min(480, h * 0.44, w * 0.5));
      stage.style.setProperty("--cw", `${Math.round(Math.max(72, R * 0.4))}px`);
      stage.style.setProperty("--persp", w <= 900 ? "920px" : "1150px");
      for (const c of cards) {
        const lat = Math.asin(c.y) * DEG;
        const lon = Math.atan2(c.x, c.z) * DEG;
        c.el.style.transform = `translate3d(${c.x * R}px, ${-c.y * R}px, ${c.z * R}px) rotateY(${lon}deg) rotateX(${lat}deg)`;
      }
      render();
    }

    function render() {
      const sx = tilt + dragY, sy = spin + dragX;
      world.style.transform = `translateZ(0px) rotateY(${sy}deg) rotateX(${sx}deg)`;
      title.style.transform = `rotateX(${-sx}deg) rotateY(${-sy}deg) translateZ(${R * 0.62}px)`;
      // Depth of each card after the same rotation (CSS applies rotateX first, then rotateY):
      // a flat black wash on the far side, and the nearest card is the one Enter opens.
      const ry = sy / DEG, rx = sx / DEG;
      const cy = Math.cos(ry), syn = Math.sin(ry), cx = Math.cos(rx), sxn = Math.sin(rx);
      let best = null, bestZ = -2;
      for (const c of cards) {
        const z1 = -c.y * sxn + c.z * cx;        // after rotateX (screen y points down, so y = -c.y)
        const zf = -c.x * syn + z1 * cy;         // after rotateY: -1 (back) … 1 (front)
        const base = 0.14 + 0.86 * Math.pow((zf + 1) / 2, 0.85);
        const dim = Math.round((1 - base) * 100) / 100;
        if (dim !== c.d) { c.el.style.setProperty("--d", String(dim)); c.d = dim; }
        if (zf > bestZ) { bestZ = zf; best = c; }
      }
      if (best !== front) {
        if (front) front.el.classList.remove("front");
        if (best) best.el.classList.add("front");
        front = best;
      }
    }

    function tick() {
      if (!running) return;
      if (!dragging) {
        dragX += velX; dragY += velY;
        velX *= 0.94; velY *= 0.94;
        if (Math.abs(velX) < 0.002) velX = 0;
        if (Math.abs(velY) < 0.002) velY = 0;
      }
      dragY = Math.max(-PITCH - tilt, Math.min(PITCH - tilt, dragY));
      render();
      frame = requestAnimationFrame(tick);
    }

    function resume() {
      if (running || $("sphere").hidden || galleryView.hidden || viewer.open) return;
      build();
      layout();
      running = true;
      frame = requestAnimationFrame(tick);
    }
    function pause() { running = false; cancelAnimationFrame(frame); }

    // drag: mouse and pen rotate at once; a short press without movement opens the card under it
    let dragging = false, startX = 0, startY = 0, lastX = 0, lastY = 0, moved = 0, downCard = null;
    stage.addEventListener("pointerdown", (e) => {
      if (viewer.open || e.button !== 0) return;
      downCard = e.target.closest(".card3d");
      dragging = true; moved = 0;
      startX = lastX = e.clientX; startY = lastY = e.clientY;
      velX = velY = 0;
      stage.setPointerCapture(e.pointerId);
      stage.classList.add("dragging");
    });
    stage.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      const dx = e.clientX - lastX, dy = e.clientY - lastY;
      lastX = e.clientX; lastY = e.clientY;
      moved = Math.max(moved, Math.hypot(e.clientX - startX, e.clientY - startY));
      dragX += dx * 0.13; dragY -= dy * 0.13;
      velX = reducedMotion.matches ? 0 : dx * 0.13;
      velY = reducedMotion.matches ? 0 : -dy * 0.13;
      if (!running) render();
    });
    const endDrag = () => {
      if (!dragging) return;
      dragging = false;
      stage.classList.remove("dragging");
      if (moved < 6 && downCard) {
        const card = cards.find((c) => c.el === downCard);
        if (card) openViewer(galleryPhotos, card.index, false, card.el);
      }
      downCard = null;
    };
    stage.addEventListener("pointerup", endDrag);
    stage.addEventListener("pointercancel", () => { dragging = false; stage.classList.remove("dragging"); downCard = null; });
    stage.addEventListener("keydown", (e) => {
      const step = 12;
      const turn = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, step], ArrowDown: [0, -step] }[e.key];
      if (turn) {
        e.preventDefault();
        if (reducedMotion.matches) { dragX += turn[0]; dragY += turn[1]; render(); }
        else { velX += turn[0] * 0.09; velY += turn[1] * 0.09; }
      } else if ((e.key === "Enter" || e.key === " ") && front) {
        e.preventDefault();
        openViewer(galleryPhotos, front.index, false, front.el);
      }
    });
    let lastSize = [0, 0];
    window.addEventListener("resize", () => {
      const size = [innerWidth, innerHeight];
      if (Math.abs(size[0] - lastSize[0]) < 20 && Math.abs(size[1] - lastSize[1]) < 20) return;
      lastSize = size;
      if (built) layout();
    });
    return { build, resume, pause, cardCount: () => cards.length };
  })();

  // ---- full-screen menu
  const menuBtn = $("menu-btn");
  const menuPanel = $("menu");
  function setMenu(open) {
    body.classList.toggle("menu-open", open);
    menuBtn.setAttribute("aria-expanded", String(open));
    menuBtn.setAttribute("aria-label", open ? "Закрыть меню" : "Открыть меню");
    menuPanel.inert = !open;
    for (const el of [$("main"), $("photo-fab-wrap")]) el.inert = open;
    if (open) menuPanel.querySelector(".menu-link").focus();
  }
  menuBtn.addEventListener("click", () => setMenu(!body.classList.contains("menu-open")));
  menuPanel.addEventListener("click", (e) => {
    const link = e.target.closest("[data-action]");
    if (!link) return;
    const action = link.dataset.action;
    setMenu(false);
    if (action === "find") { openMenu(); return; }
    if (!results.hidden) $("results-back").click();
    setView(action === "sphere" ? "sphere" : "grid");
    window.scrollTo({ top: 0, behavior: animate() ? "smooth" : "auto" });
    menuBtn.focus();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && body.classList.contains("menu-open")) { setMenu(false); menuBtn.focus(); }
  });

  // ---- cursor ring: follows the pointer (lerp) and widens over things you can click
  const dot = $("dot");
  if (animate() && dot) {
    let tx = -100, ty = -100, x = -100, y = -100;
    window.addEventListener("pointermove", (e) => {
      if (e.pointerType !== "mouse") return;
      tx = e.clientX; ty = e.clientY;
      dot.classList.add("on");
      dot.classList.toggle("wide", !!e.target.closest("a, button, .library-tile, .card3d, label"));
    }, { passive: true });
    document.documentElement.addEventListener("pointerleave", () => dot.classList.remove("on"));
    const follow = () => {
      x += (tx - x) * 0.2; y += (ty - y) * 0.2;
      dot.style.transform = `translate3d(${x}px, ${y}px, 0)`;
      requestAnimationFrame(follow);
    };
    requestAnimationFrame(follow);
  }

  function startGallery() {
    if (galleryStarted || !desktop.matches) return;
    galleryStarted = true;
    loadGallery();
  }
  desktop.addEventListener("change", () => { startGallery(); if (!desktop.matches) closeComposer(); });
  startGallery();
})();
