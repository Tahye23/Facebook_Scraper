(() => {
  const state = {
    platform: "tiktok",
    mode: "single", // single | csv
    limitMode: "count", // count | 24h
    scrapeId: null,
    pollTimer: null,
    isCsvJob: false,
  };

  const $ = (id) => document.getElementById(id);
  const apiPill = $("apiPill");
  const form = $("scrapeForm");
  const urlInput = $("urlInput");
  const maxPosts = $("maxPosts");
  const maxPostsLabel = $("maxPostsLabel");
  const forceRefresh = $("forceRefresh");
  const csvFile = $("csvFile");
  const singleFields = $("singleFields");
  const csvFields = $("csvFields");
  const csvModeBtn = $("csvModeBtn");
  const window24Btn = $("window24Btn");
  const submitBtn = $("submitBtn");
  const jobStatus = $("jobStatus");
  const quotaBanner = $("quotaBanner");
  const resultsBody = $("resultsBody");
  const resultsMeta = $("resultsMeta");
  const refreshBtn = $("refreshBtn");
  const downloadBtn = $("downloadBtn");

  function setPlatform(p) {
    state.platform = p;
    document.querySelectorAll("[data-platform]").forEach((b) => {
      b.classList.toggle("active", b.dataset.platform === p);
    });
    const fb = p === "facebook";
    csvModeBtn.disabled = fb;
    window24Btn.disabled = fb;
    if (fb && state.mode === "csv") setMode("single");
    if (fb && state.limitMode === "24h") setLimitMode("count");
    urlInput.placeholder = fb
      ? "https://www.facebook.com/page"
      : "https://www.tiktok.com/@compte";
  }

  function setMode(m) {
    state.mode = m;
    document.querySelectorAll("[data-mode]").forEach((b) => {
      b.classList.toggle("active", b.dataset.mode === m);
    });
    singleFields.classList.toggle("hidden", m !== "single");
    csvFields.classList.toggle("hidden", m !== "csv");
    urlInput.required = m === "single";
  }

  function setLimitMode(m) {
    state.limitMode = m;
    document.querySelectorAll("[data-limit]").forEach((b) => {
      b.classList.toggle("active", b.dataset.limit === m);
    });
    maxPostsLabel.classList.toggle("hidden", m === "24h");
  }

  document.querySelectorAll("[data-platform]").forEach((b) => {
    b.addEventListener("click", () => setPlatform(b.dataset.platform));
  });
  document.querySelectorAll("[data-mode]").forEach((b) => {
    b.addEventListener("click", () => {
      if (!b.disabled) setMode(b.dataset.mode);
    });
  });
  document.querySelectorAll("[data-limit]").forEach((b) => {
    b.addEventListener("click", () => {
      if (!b.disabled) setLimitMode(b.dataset.limit);
    });
  });

  function showStatus(kind, text) {
    jobStatus.classList.remove("hidden", "running", "ok", "fail");
    jobStatus.classList.add(kind);
    jobStatus.textContent = text;
  }

  function showQuota(msg) {
    quotaBanner.classList.remove("hidden");
    quotaBanner.textContent = msg;
  }

  function hideQuota() {
    quotaBanner.classList.add("hidden");
    quotaBanner.textContent = "";
  }

  function fmt(n) {
    if (n === null || n === undefined || n === "") return "—";
    const x = Number(n);
    return Number.isFinite(x) ? x.toLocaleString("fr-FR") : String(n);
  }

  function renderResults(payload) {
    const rows = payload.results || [];
    resultsMeta.textContent = `Job ${payload.scrape_id || state.scrapeId || "—"} · ${payload.status || "?"} · ${payload.count ?? rows.length} résultat(s)`;
    if (!rows.length) {
      resultsBody.innerHTML = `<tr class="empty"><td colspan="9">Aucun résultat pour l'instant.</td></tr>`;
      return;
    }
    resultsBody.innerHTML = rows
      .map((r) => {
        const m = r.metrics || {};
        const tags = Array.isArray(r.hashtags) ? r.hashtags.join(" ") : "";
        const report = r.video_report || r.videoReport || null;
        let gemini = "—";
        if (report && typeof report === "object") {
          const summary = Array.isArray(report.executive_summary)
            ? report.executive_summary.join(" ")
            : report.executive_summary || "";
          const themes = Array.isArray(report.themes) ? report.themes.join(", ") : "";
          const conf = report.confidence_and_limits || {};
          gemini = `<details><summary>${escapeHtml(report.sentiment || "voir")} · ${escapeHtml(conf.level || "?")}</summary>
            <div class="desc">${escapeHtml(summary || "(pas de résumé)")}</div>
            <div class="hint">Thèmes: ${escapeHtml(themes || "—")}</div>
          </details>`;
        }
        return `<tr>
          <td>${escapeHtml(r.author || "")}</td>
          <td class="desc">${escapeHtml(r.text_content || r.textContent || "")}</td>
          <td class="desc">${escapeHtml(tags || "—")}</td>
          <td class="num">${fmt(m.likes)}</td>
          <td class="num">${fmt(m.comments)}</td>
          <td class="num">${fmt(m.shares)}</td>
          <td class="num">${fmt(m.views)}</td>
          <td class="num">${escapeHtml((r.published_at || r.publishedAt || "").toString().slice(0, 19))}</td>
          <td class="gemini">${gemini}</td>
        </tr>`;
      })
      .join("");
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function pollJob() {
    if (!state.scrapeId) return;
    try {
      const jobRes = await fetch(`/scrape/${state.scrapeId}`);
      const job = await jobRes.json();
      const status = (job.status || "").toUpperCase();
      const reason = job.error_reason || job.errorReason || "";
      const msg = job.error_message || job.errorMessage || job.message || "";

      const isQuota = reason === "QUOTA_EXCEEDED" || /quota.*apify/i.test(msg || "");
      if (isQuota) {
        showQuota(msg || "Quota Apify journalier atteint.");
        showStatus(
          status === "PARTIAL_SUCCESS" ? "ok" : "fail",
          status === "PARTIAL_SUCCESS"
            ? "Terminé (partiel) — quota atteint"
            : "Échec — quota atteint"
        );
      } else if (status === "FAILED") {
        hideQuota();
        showStatus("fail", msg || "Échec du scrape");
      } else if (status === "SUCCESS" || status === "PARTIAL_SUCCESS") {
        hideQuota();
        showStatus("ok", status === "PARTIAL_SUCCESS" ? "Terminé (succès partiel)" : "Terminé");
      } else {
        hideQuota();
        showStatus("running", `En cours… (${status || "QUEUED"})`);
      }

      const res = await fetch(`/scrape/${state.scrapeId}/results`);
      const payload = await res.json();
      // Propagate error_reason from results endpoint too
      if (payload.error_reason === "QUOTA_EXCEEDED") {
        showQuota(payload.message || msg || "Quota Apify journalier atteint.");
      }
      renderResults(payload);

      const done = ["SUCCESS", "PARTIAL_SUCCESS", "FAILED"].includes(status);
      // Rapport HTML 24h/CSV (metadata.htmlPath) — pas le CSV brut.
      const meta = payload.metadata || job.metadata || {};
      const hasReport = !!(payload.report_url || meta.htmlPath || meta.html_path);
      const canDownload = state.isCsvJob && done && status !== "FAILED" && hasReport;
      downloadBtn.classList.toggle("hidden", !canDownload);
      if (canDownload) {
        downloadBtn.href = payload.report_url || `/scrape/${state.scrapeId}/report`;
        downloadBtn.removeAttribute("download");
        downloadBtn.textContent = "Télécharger le rapport";
      }
      if (done) {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
        submitBtn.disabled = false;
      }
    } catch (e) {
      showStatus("fail", `Erreur polling: ${e.message}`);
    }
  }

  function startPolling(scrapeId, isCsv) {
    state.scrapeId = scrapeId;
    state.isCsvJob = !!isCsv;
    refreshBtn.disabled = false;
    downloadBtn.classList.add("hidden");
    if (state.pollTimer) clearInterval(state.pollTimer);
    pollJob();
    state.pollTimer = setInterval(pollJob, 2500);
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    hideQuota();
    submitBtn.disabled = true;
    showStatus("running", "Envoi du job…");

    try {
      if (state.mode === "csv") {
        const file = csvFile.files && csvFile.files[0];
        if (!file) throw new Error("Choisissez un fichier CSV");
        const fd = new FormData();
        fd.append("file", file);
        const res = await fetch("/scrape/csv-report", { method: "POST", body: fd });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || data.message || res.statusText);
        const id = data.scrape_id || data.scrapeId;
        showStatus("running", `CSV accepté · ${id}`);
        startPolling(id, true);
      } else if (state.limitMode === "24h" && state.platform === "tiktok") {
        // Profil unique en 24h: on reutilise csv-report avec un CSV 1 ligne
        const url = urlInput.value.trim();
        const blob = new Blob([`url\n${url}\n`], { type: "text/csv" });
        const fd = new FormData();
        fd.append("file", blob, "single.csv");
        const res = await fetch("/scrape/csv-report", { method: "POST", body: fd });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || data.message || res.statusText);
        const id = data.scrape_id || data.scrapeId;
        showStatus("running", `Job 24h accepté · ${id}`);
        startPolling(id, true);
      } else {
        const body = {
          url: urlInput.value.trim(),
          platform: state.platform,
          max_posts: Number(maxPosts.value) || 5,
          force_refresh: !!forceRefresh.checked,
        };
        const res = await fetch("/scrape", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || data.message || res.statusText);
        const id = data.scrape_id || data.scrapeId;
        const st = (data.status || "").toUpperCase();
        if (st === "SUCCESS") {
          showStatus("ok", `Servi depuis le cache · ${id}`);
        } else {
          showStatus("running", `Job accepté · ${id}`);
        }
        startPolling(id, false);
        loadDashboard();
      }
    } catch (err) {
      submitBtn.disabled = false;
      showStatus("fail", err.message || String(err));
    }
  });

  refreshBtn.addEventListener("click", () => pollJob());

  function money(n) {
    const x = Number(n);
    if (!Number.isFinite(x)) return "—";
    return `$${x.toFixed(2)}`;
  }

  function pct(used, max) {
    if (!max || max <= 0) return 0;
    return Math.min(100, Math.round((used / max) * 100));
  }

  async function loadDashboard() {
    try {
      const [usageRes, jobsRes] = await Promise.all([
        fetch("/scrape/usage"),
        fetch("/scrape/jobs?limit=8"),
      ]);
      apiPill.textContent = jobsRes.ok ? "API OK" : `API ${jobsRes.status}`;
      apiPill.classList.toggle("ok", jobsRes.ok);

      if (usageRes.ok) {
        const u = await usageRes.json();
        const apify = u.apify || {};
        const daily = u.daily || {};
        if (apify.available) {
          const used = Number(apify.monthly_usage_usd) || 0;
          const max = Number(apify.max_monthly_usage_usd) || 5;
          $("apifyAmount").textContent = `${money(used)} / ${money(max)}`;
          $("apifyBar").style.width = `${pct(used, max)}%`;
          const ram = Number(apify.ram_mb) || 0;
          const ramMax = Number(apify.max_ram_mb) || 16384;
          $("apifyHint").textContent = `RAM ~${Math.round(ram)} MB / ${Math.round(ramMax / 1024)} GB · mois en cours`;
        } else {
          $("apifyAmount").textContent = "N/A";
          $("apifyHint").textContent = apify.error || "Token Apify non configuré sur le gateway";
        }
        const count = Number(daily.count) || 0;
        const limit = Number(daily.limit) || 33;
        $("dailyAmount").textContent = `${count} / ${limit}`;
        $("dailyBar").style.width = `${pct(count, limit)}%`;
        $("dailyHint").textContent = `Reste ${Math.max(0, limit - count)} vidéo(s) · ${daily.date || "aujourd'hui"} (UTC)`;
      }

      if (jobsRes.ok) {
        const payload = await jobsRes.json();
        const jobs = payload.jobs || [];
        const list = $("recentList");
        if (!jobs.length) {
          list.innerHTML = `<li class="hint">Aucun scrape récent.</li>`;
        } else {
          list.innerHTML = jobs
            .map((j) => {
              const id = j.scrape_id || j.scrapeId;
              const url = j.url || "";
              const status = j.status || "?";
              const platform = j.platform || "";
              const created = (j.created_at || j.createdAt || "").toString().slice(0, 19);
              return `<li><button type="button" data-scrape-id="${escapeHtml(id)}">
                <div>${escapeHtml(platform)} · ${escapeHtml(status)}</div>
                <div class="recent-meta">${escapeHtml(url.slice(0, 60))} · ${escapeHtml(created)}</div>
              </button></li>`;
            })
            .join("");
          list.querySelectorAll("[data-scrape-id]").forEach((btn) => {
            btn.addEventListener("click", () => {
              const id = btn.getAttribute("data-scrape-id");
              const isCsv = /csv-batch/i.test(btn.querySelector(".recent-meta")?.textContent || "");
              showStatus("running", `Rechargement · ${id}`);
              startPolling(id, isCsv);
            });
          });
        }
      }
    } catch (e) {
      apiPill.textContent = "API hors ligne";
      $("apifyHint").textContent = e.message || "Erreur usage";
    }
  }

  loadDashboard();
})();
