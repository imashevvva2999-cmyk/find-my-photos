// Admin event page: upload photos in small batches with a progress bar, then follow the
// background processing. Only changed photos are fetched (cursor), polling pauses while the
// tab is hidden, and every failure is shown to the organiser.
(() => {
  // Ask before destructive actions (delete event, new link).
  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (e) => {
      if (!confirm(form.dataset.confirm)) e.preventDefault();
    });
  });

  const root = document.getElementById("admin-event");
  if (!root) return;

  const eventId = root.dataset.eventId;
  const maxMb = Number(root.dataset.maxMb);
  const maxBytes = maxMb * 1024 * 1024;
  const FILES_PER_REQUEST = Number(root.dataset.maxFiles) || 5;
  const ALLOWED = [".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"];

  const $ = (id) => document.getElementById(id);
  const input = $("photo-input");
  const dropzone = $("dropzone");
  const uploadBox = $("upload-box");
  const uploadBar = $("upload-bar");
  const uploadText = $("upload-text");
  const rejectedList = $("rejected");
  const processBar = $("process-bar");
  const processText = $("process-text");
  const grid = $("photo-grid");
  const seconds = (ms) => `${(ms / 1000).toFixed(2).replace(".", ",")} с`;
  // Russian plural forms: plural(5, "фотография", "фотографии", "фотографий") → "фотографий"
  const plural = (n, one, few, many) => {
    const m10 = n % 10, m100 = n % 100;
    return m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? few : many;
  };
  const photos = (n) => `${n} ${plural(n, "фотография", "фотографии", "фотографий")}`;

  // ---- copy link
  $("copy-link").addEventListener("click", async (e) => {
    const link = $("visitor-link");
    try {
      await navigator.clipboard.writeText(link.value);
    } catch {
      link.select();
      document.execCommand("copy");
    }
    e.target.textContent = "Скопировано!";
    setTimeout(() => (e.target.textContent = "Копировать"), 1500);
  });

  // ---- choosing files
  input.addEventListener("change", () => {
    enqueueUpload([...input.files]);
    input.value = "";
  });
  ["dragenter", "dragover"].forEach((t) =>
    dropzone.addEventListener(t, (e) => { e.preventDefault(); dropzone.classList.add("dragging"); }));
  ["dragleave", "drop"].forEach((t) => dropzone.addEventListener(t, () => dropzone.classList.remove("dragging")));
  dropzone.addEventListener("drop", (e) => {
    e.preventDefault();
    enqueueUpload([...e.dataTransfer.files]);
  });

  function showRejected(items) {
    for (const item of items) {
      const li = document.createElement("li");
      li.textContent = `${item.name}: ${item.reason}`;
      rejectedList.appendChild(li);
    }
    rejectedList.hidden = rejectedList.children.length === 0;
  }

  // Check type and size in the browser first, so bad files are never sent.
  function validate(files) {
    const good = [];
    const bad = [];
    for (const f of files) {
      const ext = f.name.includes(".") ? f.name.slice(f.name.lastIndexOf(".")).toLowerCase() : "";
      if (!ALLOWED.includes(ext)) bad.push({ name: f.name, reason: "Неподдерживаемый тип файла (нужен JPG, PNG, WEBP или HEIC)." });
      else if (f.size === 0) bad.push({ name: f.name, reason: "Файл пустой." });
      else if (f.size > maxBytes) bad.push({ name: f.name, reason: `Больше ${maxMb} МБ.` });
      else good.push(f);
    }
    return { good, bad };
  }

  let chain = Promise.resolve();
  function enqueueUpload(files) {
    chain = chain.then(() => upload(files));
  }

  async function upload(files) {
    const { good, bad } = validate(files);
    showRejected(bad);
    if (good.length === 0) return;

    uploadBox.hidden = false;
    const uploadStart = performance.now();
    uploading = true;
    startWallTimer();
    const totalBytes = good.reduce((sum, f) => sum + f.size, 0);
    let sentBytes = 0;
    let accepted = 0;
    const draw = (extra) => {
      const pct = Math.min(100, ((sentBytes + extra) / totalBytes) * 100);
      uploadBar.style.width = `${pct}%`;
      uploadText.textContent = `${Math.round(pct)}% · сохранено ${accepted} из ${good.length}`;
    };
    draw(0);

    for (let i = 0; i < good.length; i += FILES_PER_REQUEST) {
      const batch = good.slice(i, i + FILES_PER_REQUEST);
      const batchBytes = batch.reduce((sum, f) => sum + f.size, 0);
      try {
        const result = await sendBatch(batch, (loaded) => draw(Math.min(loaded, batchBytes)));
        accepted += result.accepted.length;
        showRejected(result.rejected);
      } catch (err) {
        showRejected(batch.map((f) => ({ name: f.name, reason: err.message })));
        if (err.loginRequired) { location.href = "/admin/login"; return; }
        if (err.stopAll) {           // e.g. disk full or event deleted: the rest would fail too
          showRejected(good.slice(i + FILES_PER_REQUEST).map((f) => ({ name: f.name, reason: "Не отправлено: " + err.message })));
          break;
        }
      }
      sentBytes += batchBytes;
      draw(0);
      refreshStatus();
    }
    uploadText.textContent = `Готово · сохранено ${accepted} из ${good.length}`;
    $("t-upload").textContent = `${seconds(performance.now() - uploadStart)} на ${photos(accepted)}`;
    uploading = false;
    refreshStatus();
  }

  // XMLHttpRequest (not fetch) because it reports upload progress.
  function sendBatch(files, onProgress) {
    return new Promise((resolve, reject) => {
      const form = new FormData();
      files.forEach((f) => form.append("files", f, f.name));
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `/admin/api/events/${eventId}/photos`);
      xhr.upload.onprogress = (e) => onProgress(e.loaded);
      xhr.onload = () => {
        let body = {};
        try { body = JSON.parse(xhr.responseText); } catch { /* not JSON */ }
        if (xhr.status === 200) return resolve(body);
        const err = new Error(body.message || `Не удалось загрузить (ошибка ${xhr.status}). Попробуйте ещё раз.`);
        err.loginRequired = xhr.status === 401;
        err.stopAll = [404, 507].includes(xhr.status);
        reject(err);
      };
      xhr.onerror = () => reject(new Error("Ошибка сети. Приложение всё ещё запущено?"));
      xhr.send(form);
    });
  }

  // ---- timers: upload, upload + processing (live), server-side face processing
  let uploading = false;
  let wallStart = null;
  let wallTick = null;

  function startWallTimer() {
    if (wallStart !== null) return; // already running for an earlier batch of files
    wallStart = performance.now();
    $("t-wall").classList.add("running");
    wallTick = setInterval(() => ($("t-wall").textContent = seconds(performance.now() - wallStart)), 50);
  }

  function stopWallTimerIfDone(s) {
    if (wallStart === null || uploading || s.pending + s.processing > 0) return;
    clearInterval(wallTick);
    $("t-wall").textContent = seconds(performance.now() - wallStart);
    $("t-wall").classList.remove("running");
    wallStart = null;
  }

  // ---- processing progress + photo grid (incremental)
  const tiles = new Map(); // photo id -> tile element
  const STATUS_TEXT = { pending: "В очереди…", processing: "Ищем лица…", error: "Ошибка" };
  let cursor = null;
  let pollTimer = null;
  let polling = false;
  let generation = 0; // bumped when the grid is reset; answers to older requests are ignored

  async function refreshStatus() {
    clearTimeout(pollTimer);
    if (polling) return;
    polling = true;
    const gen = generation;
    let busy = false;
    try {
      let more = true;
      while (more && gen === generation) {
        const url = `/admin/api/events/${eventId}/status` + (cursor ? `?since=${encodeURIComponent(cursor)}` : "");
        const res = await fetch(url);
        if (res.status === 401) { location.href = "/admin/login"; return; }
        if (!res.ok) throw new Error(`status ${res.status}`);
        const s = await res.json();
        if (gen !== generation) break; // the grid was reset while this request was running
        cursor = s.cursor;
        more = s.has_more;
        busy = s.pending + s.processing > 0;
        render(s);
      }
    } catch {
      processText.textContent = "Нет связи с приложением. Пробуем снова…";
    } finally {
      polling = false;
    }
    if (gen !== generation) { refreshStatus(); return; } // start again from the reset state
    if (document.hidden) return; // resumes on visibilitychange
    pollTimer = setTimeout(refreshStatus, wallStart !== null ? 500 : busy ? 1000 : 10000);
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshStatus(); });

  function render(s) {
    const finished = s.done + s.error;
    processBar.style.width = s.total ? `${(finished / s.total) * 100}%` : "0";
    processText.textContent = s.total === 0
      ? "Фотографий пока нет"
      : `Обработано ${finished} из ${s.total} · найдено ${s.faces} ${plural(s.faces, "лицо", "лица", "лиц")}${s.error ? ` · ошибок: ${s.error}` : ""}`;
    if (s.timed_photos) {
      const avg = s.processing_seconds / s.timed_photos;
      $("t-process").textContent = `${seconds(s.processing_seconds * 1000)} на ${photos(s.timed_photos)} (${seconds(avg * 1000)} на фото)`;
    }
    $("failed-box").hidden = s.error === 0;
    $("failed-text").textContent = `Не удалось обработать: ${photos(s.error)}. `;
    stopWallTimerIfDone(s);

    for (const p of s.photos) {
      const fresh = makeTile(p);
      const old = tiles.get(p.id);
      if (old) old.replaceWith(fresh);
      else grid.prepend(fresh);  // newest first
      tiles.set(p.id, fresh);
    }
  }

  function makeTile(p) {
    const tile = document.createElement("figure");
    tile.className = "tile";
    tile.dataset.status = p.status;

    if (p.status === "done") {
      const link = document.createElement("a");
      link.className = "tile-img";
      link.href = `/admin/photos/${p.id}/preview`;
      link.target = "_blank";
      const img = document.createElement("img");
      img.src = `/admin/photos/${p.id}/thumb`;
      img.alt = p.original_name;
      img.loading = "lazy";
      link.appendChild(img);
      tile.appendChild(link);
    } else {
      const ph = document.createElement("div");
      ph.className = "tile-img tile-placeholder";
      ph.textContent = STATUS_TEXT[p.status];
      tile.appendChild(ph);
    }

    const body = document.createElement("figcaption");
    body.className = "tile-body";
    const name = document.createElement("span");
    name.className = "tile-name";
    name.textContent = p.original_name;
    name.title = p.original_name;
    const status = document.createElement("span");
    status.className = `status-${p.status}`;
    status.textContent = p.status === "done"
      ? `✓ ${p.face_count} ${plural(p.face_count, "лицо", "лица", "лиц")}`
      : p.status === "error" ? `✗ ${p.error || "Ошибка"}` : STATUS_TEXT[p.status];
    if (p.status === "pending" && p.error) status.textContent = "Скоро повторим…";
    body.append(name, status);
    tile.appendChild(body);

    const del = document.createElement("button");
    del.className = "tile-delete";
    del.type = "button";
    del.title = "Удалить фото";
    del.setAttribute("aria-label", `Удалить ${p.original_name}`);
    del.textContent = "×";
    del.addEventListener("click", async () => {
      if (!confirm(`Удалить «${p.original_name}» и данные о лицах на нём?`)) return;
      try {
        const res = await fetch(`/admin/api/photos/${p.id}/delete`, { method: "POST" });
        if (res.status === 401) { location.href = "/admin/login"; return; }
        if (!res.ok && res.status !== 404) throw new Error();
        generation++;            // ignore any status answer that is still on its way
        cursor = null;           // reload the whole grid and recount
        grid.innerHTML = "";
        tiles.clear();
        refreshStatus();
      } catch {
        alert("Не удалось удалить фото. Попробуйте ещё раз.");
      }
    });
    tile.appendChild(del);
    return tile;
  }

  $("retry-failed").addEventListener("click", async (e) => {
    e.target.disabled = true;
    try {
      const res = await fetch(`/admin/api/events/${eventId}/retry`, { method: "POST" });
      if (res.status === 401) { location.href = "/admin/login"; return; }
      if (!res.ok) throw new Error();
    } catch {
      alert("Не удалось перезапустить обработку. Попробуйте ещё раз.");
    } finally {
      e.target.disabled = false;
      refreshStatus();
    }
  });

  refreshStatus();
})();
