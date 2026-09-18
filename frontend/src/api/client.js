export async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.body);
  }
  const res = await fetch(`/api${path}`, {
    ...options,
    headers,
    credentials: "include",
  });
  if (res.status === 401 && !path.startsWith("/auth/")) {
    const here = window.location.pathname;
    if (!here.startsWith("/login") && !here.startsWith("/setup")) {
      window.location.href = "/login";
    }
    throw new Error("Unauthorized");
  }
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    const data = await res.json();
    if (!res.ok) {
      const msg = data.detail || data.error || JSON.stringify(data);
      throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    }
    return data;
  }
  if (!res.ok) {
    throw new Error(await res.text());
  }
  return res;
}

export async function checkSession() {
  try {
    return await api("/auth/me");
  } catch {
    return null;
  }
}
