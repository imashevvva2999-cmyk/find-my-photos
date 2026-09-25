// Organiser passkeys (WebAuthn): sign in on the login page, add one in the organiser area.
// The server sends the options as JSON (binary fields in base64url) and checks the answer;
// this file only converts between JSON and the browser's passkey API.
(() => {
  const $ = (id) => document.getElementById(id);
  const supported = !!(window.PublicKeyCredential && navigator.credentials);

  const b64urlToBuf = (s) => {
    const bin = atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4));
    return Uint8Array.from(bin, (c) => c.charCodeAt(0)).buffer;
  };
  const bufToB64url = (buf) =>
    btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

  function creationOptions(o) {
    return { ...o, challenge: b64urlToBuf(o.challenge), user: { ...o.user, id: b64urlToBuf(o.user.id) },
             excludeCredentials: (o.excludeCredentials || []).map((c) => ({ ...c, id: b64urlToBuf(c.id) })) };
  }
  function requestOptions(o) {
    return { ...o, challenge: b64urlToBuf(o.challenge),
             allowCredentials: (o.allowCredentials || []).map((c) => ({ ...c, id: b64urlToBuf(c.id) })) };
  }
  function credentialJSON(cred) {
    const r = cred.response;
    const response = { clientDataJSON: bufToB64url(r.clientDataJSON) };
    if (r.attestationObject) {
      response.attestationObject = bufToB64url(r.attestationObject);
      if (r.getTransports) response.transports = r.getTransports();
    } else {
      response.authenticatorData = bufToB64url(r.authenticatorData);
      response.signature = bufToB64url(r.signature);
      if (r.userHandle) response.userHandle = bufToB64url(r.userHandle);
    }
    return { id: cred.id, rawId: bufToB64url(cred.rawId), type: cred.type, response,
             clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {},
             authenticatorAttachment: cred.authenticatorAttachment || undefined };
  }

  async function post(url, body) {
    const res = await fetch(url, { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: body === undefined ? "{}" : JSON.stringify(body) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.message || "Что-то пошло не так. Попробуйте ещё раз.");
    return data;
  }

  // The browser's own "cancelled" / "not allowed" is not an error worth alarming about.
  const cancelled = (err) => err && (err.name === "NotAllowedError" || err.name === "AbortError");

  // ---- login page
  const loginBtn = $("passkey-login");
  if (loginBtn) {
    const error = $("passkey-error");
    const show = (text) => { error.textContent = text; error.hidden = !text; };
    if (!supported) {
      loginBtn.disabled = true;
      show("Этот браузер не поддерживает ключи доступа. Войдите по паролю.");
    }
    loginBtn.addEventListener("click", async () => {
      show("");
      loginBtn.disabled = true;
      try {
        const options = await post("/admin/passkey/login/options");
        const cred = await navigator.credentials.get({ publicKey: requestOptions(options) });
        const result = await post("/admin/passkey/login/verify", credentialJSON(cred));
        location.assign(result.next || "/admin");
        return;
      } catch (err) {
        show(cancelled(err) ? "Вход отменён. Попробуйте ещё раз или войдите по паролю." : err.message);
      }
      loginBtn.disabled = false;
    });
  }

  // ---- organiser area: add a passkey on this device
  const addBtn = $("passkey-add");
  if (addBtn) {
    const status = $("passkey-status");
    const show = (kind, text) => { status.className = `alert small alert-${kind}`; status.textContent = text; status.hidden = !text; };
    if (!supported) {
      addBtn.disabled = true;
      show("info", "Этот браузер не поддерживает ключи доступа.");
    }
    addBtn.addEventListener("click", async () => {
      show("info", "");
      addBtn.disabled = true;
      try {
        const options = await post("/admin/api/passkeys/options");
        const cred = await navigator.credentials.create({ publicKey: creationOptions(options) });
        await post("/admin/api/passkeys", { credential: credentialJSON(cred), name: $("passkey-name").value });
        show("success", "Ключ доступа добавлен. Теперь можно входить без пароля.");
        setTimeout(() => location.reload(), 900);
        return;
      } catch (err) {
        show("error", cancelled(err) ? "Добавление отменено." :
          err.name === "InvalidStateError" ? "На этом устройстве ключ доступа уже добавлен." : err.message);
      }
      addBtn.disabled = false;
    });
  }
})();
