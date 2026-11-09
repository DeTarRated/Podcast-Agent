(function () {
  "use strict";

  const root = document.getElementById("app");
  const notice = document.getElementById("notice");
  const playbackDock = document.getElementById("playback-dock");
  const playbackAudio = document.getElementById("playback-audio");
  const playbackTitle = document.getElementById("playback-title");
  const playbackClose = document.getElementById("playback-close");
  const operationKeys = new Map();
  let playbackSource = null;
  let playerCleanup = null;
  let currentDetail = null;
  let currentLibraryDetail = null;
  let editing = null;

  function element(tag, options = {}, text) {
    const node = document.createElement(tag);
    if (options.className) node.className = options.className;
    if (options.attributes) {
      for (const [key, value] of Object.entries(options.attributes)) node.setAttribute(key, value);
    }
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function showNotice(message, kind = "error") {
    notice.textContent = message;
    notice.className = `notice ${kind}`;
    notice.hidden = false;
  }

  function clearNotice() {
    notice.hidden = true;
    notice.textContent = "";
  }

  function mountPlayback(source, title) {
    if (playbackSource !== source) {
      playbackSource = source;
      playbackAudio.src = source;
      playbackAudio.load();
      playbackAudio.currentTime = 0;
    }
    playbackTitle.textContent = title;
    playbackDock.hidden = false;
    return playbackAudio;
  }

  playbackClose.addEventListener("click", () => {
    playbackAudio.pause();
    playbackAudio.removeAttribute("src");
    playbackAudio.load();
    playbackSource = null;
    playbackDock.hidden = true;
    if (playerCleanup) playerCleanup();
    playerCleanup = null;
  });

  function formatTime(milliseconds) {
    if (!Number.isFinite(milliseconds)) return "未知时间";
    const total = Math.floor(milliseconds / 1000);
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const seconds = total % 60;
    return hours > 0
      ? `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
      : `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  }

  function formatDate(value) {
    return value ? new Date(value).toLocaleString("zh-CN") : "未设置";
  }

  async function api(path, options = {}) {
    const headers = { Accept: "application/json" };
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    if (options.key) headers["Idempotency-Key"] = options.key;
    const response = await fetch(path, {
      method: options.method || "GET",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      credentials: "same-origin",
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      const error = new Error(payload?.error?.message || `请求失败 (${response.status})`);
      error.status = response.status;
      error.code = payload?.error?.code;
      error.details = payload?.error?.details;
      throw error;
    }
    return payload;
  }

  function keyFor(scope) {
    if (!operationKeys.has(scope)) operationKeys.set(scope, crypto.randomUUID());
    return operationKeys.get(scope);
  }

  function mutation(path, scope, body) {
    return api(path, { method: "POST", body, key: keyFor(scope) });
  }

  function succeeded(scope) {
    operationKeys.delete(scope);
  }

  function handleError(error) {
    if (error.code === "override_conflict") {
      showNotice("修改已保存，但存在映射冲突；请查看冲突信息并处理后审批。", "warning");
      editing = null;
      if (currentDetail) loadEpisode(currentDetail.episode_id);
      return;
    }
    showNotice(error.message || "操作失败");
    if (error.code === "stale_revision" && currentDetail) {
      editing = null;
      loadEpisode(currentDetail.episode_id);
    }
  }

  function renderLoading() {
    root.replaceChildren(element("p", { className: "muted" }, "正在加载…"));
  }

  function costLabel(item) {
    if (item.cost_amount === null) return "未对账";
    const source = item.cost_source === "ledger_estimate" ? "账本估算" : "模型返回";
    return `$${Number(item.cost_amount).toFixed(6)} · ${source}`;
  }

  function usageQuery(params) {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== "" && value !== undefined && value !== null) query.set(key, String(value));
    }
    const text = query.toString();
    return text ? `?${text}` : "";
  }

  async function loadUsage(params = {}) {
    if (playerCleanup) playerCleanup();
    playerCleanup = null;
    currentDetail = null;
    currentLibraryDetail = null;
    editing = null;
    renderLoading();
    try {
      const result = await api(`/api/usage${usageQuery(params)}`);
      const heading = element("div", { className: "section-heading" });
      heading.append(element("h1", {}, "用量与费用"));
      const cards = element("div", { className: "usage-cards" });
      const cardData = [
        ["估算总额", `$${Number(result.summary.total_amount).toFixed(6)}`],
        ["节目数", String(result.summary.episode_count)],
        ["调用次数", String(result.summary.call_count)],
        ["待对账", String(result.summary.unknown_count)],
        ["失败", String(result.summary.failed_count)],
        ["无金额记录", String(result.summary.unpriced_count)],
      ];
      for (const [label, value] of cardData) {
        const card = element("div", { className: "usage-card" });
        card.append(element("span", { className: "muted" }, label), element("strong", {}, value));
        cards.append(card);
      }

      const filter = element("form", { className: "panel usage-filter" });
      const episodeSelect = element("select", { attributes: { name: "episodeId" } });
      episodeSelect.append(element("option", { attributes: { value: "" } }, "全部节目"));
      for (const episode of result.filters.episodes) {
        const option = element("option", { attributes: { value: String(episode.id) } }, episode.title || episode.guid);
        option.selected = String(params.episodeId || "") === String(episode.id);
        episodeSelect.append(option);
      }
      const purposeSelect = element("select", { attributes: { name: "purpose" } });
      purposeSelect.append(element("option", { attributes: { value: "" } }, "全部用途"));
      for (const purpose of result.filters.purposes) {
        const option = element("option", { attributes: { value: purpose } }, purpose);
        option.selected = params.purpose === purpose;
        purposeSelect.append(option);
      }
      const statusSelect = element("select", { attributes: { name: "status" } });
      statusSelect.append(element("option", { attributes: { value: "" } }, "全部状态"));
      for (const status of result.filters.statuses) {
        const option = element("option", { attributes: { value: status } }, status);
        option.selected = params.status === status;
        statusSelect.append(option);
      }
      const providerSelect = element("select", { attributes: { name: "provider" } });
      providerSelect.append(element("option", { attributes: { value: "" } }, "全部供应商"));
      for (const provider of result.filters.providers) {
        const option = element("option", { attributes: { value: provider } }, provider);
        option.selected = params.provider === provider;
        providerSelect.append(option);
      }
      const submit = element("button", { className: "secondary", attributes: { type: "submit" } }, "筛选");
      const reset = element("button", { className: "secondary", attributes: { type: "button" } }, "重置");
      reset.addEventListener("click", () => loadUsage());
      filter.append(episodeSelect, purposeSelect, statusSelect, providerSelect, submit, reset);
      filter.addEventListener("submit", (event) => {
        event.preventDefault();
        loadUsage({
          episodeId: episodeSelect.value,
          purpose: purposeSelect.value,
          status: statusSelect.value,
          provider: providerSelect.value,
        });
      });

      const tableWrap = element("div", { className: "usage-table-wrap" });
      const table = element("table", { className: "usage-table" });
      const thead = element("thead", {});
      const header = element("tr", {});
      for (const label of ["时间", "节目", "用途 / 模型", "用量", "费用", "状态", "call_ref"]) {
        header.append(element("th", {}, label));
      }
      thead.append(header);
      const tbody = element("tbody", {});
      for (const item of result.items) {
        const row = element("tr", { className: item.status === "unknown" || item.status === "failed" ? "usage-alert" : "" });
        row.append(element("td", {}, formatDate(item.triggered_at)));
        const episodeCell = element("td", {});
        if (item.episode_id) {
          episodeCell.append(element("a", { attributes: { href: `#/episodes/${item.episode_id}` } }, item.title || item.guid));
        } else {
          episodeCell.textContent = "不绑期数";
        }
        row.append(episodeCell);
        const modelCell = element("td", {});
        modelCell.append(
          element("div", {}, `${item.purpose} · ${item.provider || "未知"}`),
          element("div", { className: "muted" }, item.model || "未知模型"),
        );
        row.append(modelCell);
        row.append(element("td", { className: "usage-basis" }, item.usage_basis || "未记录"));
        row.append(element("td", { className: "cost-cell" }, costLabel(item)));
        row.append(element("td", {}, item.status));
        row.append(element("td", { className: "call-ref" }, item.call_ref));
        tbody.append(row);
      }
      if (result.items.length === 0) {
        const empty = element("tr", {});
        const cell = element("td", { attributes: { colspan: "7" } }, "没有符合条件的消费记录。");
        empty.append(cell);
        tbody.append(empty);
      }
      table.append(thead, tbody);
      tableWrap.append(table);
      const r2 = element("p", { className: "muted" });
      r2.append(
        element("span", {}, `${result.r2_note} `),
        element("a", { attributes: { href: "https://dash.cloudflare.com/", target: "_blank", rel: "noopener" } }, "打开 Cloudflare"),
      );
      const parts = [heading, cards, filter, tableWrap, r2];
      if (result.truncated) parts.push(element("p", { className: "muted" }, "明细仅显示前 500 条，汇总仍覆盖全部筛选结果。"));
      root.replaceChildren(...parts);
    } catch (error) {
      handleError(error);
      root.replaceChildren(element("p", { className: "empty" }, "消费记录加载失败。"));
    }
  }

  async function renderLibrary() {
    if (playerCleanup) playerCleanup();
    playerCleanup = null;
    currentDetail = null;
    currentLibraryDetail = null;
    editing = null;
    renderLoading();
    try {
      const result = await api("/api/library/episodes");
      const heading = element("div", { className: "section-heading" });
      heading.append(
        element("h1", {}, "私人播客库"),
        element("span", { className: "muted" }, `${result.count} 期已发布`),
      );
      const list = element("div", { className: "episode-list" });
      if (result.items.length === 0) list.append(element("p", { className: "empty" }, "还没有已发布节目。"));
      for (const episode of result.items) {
        const link = element("a", { className: "episode-card", attributes: { href: `#/library/episodes/${episode.id}` } });
        const body = element("div", { className: "episode-card-body" });
        body.append(
          element("h2", {}, episode.title || episode.guid),
          element("p", { className: "muted" }, `${episode.feed} · ${formatDate(episode.published_at)}`),
          element("p", { className: "summary-text" }, episode.summary || "暂无看点摘要"),
        );
        if (episode.episode_status === "reviewing") {
          body.append(element("span", { className: "badge" }, "有修订待审核"));
        }
        link.append(body);
        list.append(link);
      }
      root.replaceChildren(heading, list);
    } catch (error) {
      handleError(error);
      root.replaceChildren(element("p", { className: "empty" }, "节目库加载失败。"));
    }
  }

  function renderPublishedEpisode(detail) {
    currentLibraryDetail = detail;
    const heading = element("div", { className: "episode-heading" });
    const title = element("div", {});
    title.append(
      element("h1", {}, detail.episode.title || detail.episode.guid),
      element("p", { className: "muted" }, `${detail.episode.feed} · ${formatDate(detail.episode.published_at)}`),
    );
    heading.append(title, element("span", { className: "badge" }, "已发布"));
    const audio = mountPlayback(detail.audio_url, detail.episode.title || detail.episode.guid);
    const actions = element("div", { className: "actions panel" });
    const download = element("a", { className: "download-link", attributes: { href: detail.subtitle_url, download: "" } }, "下载中文 SRT");
    actions.append(download);
    if (detail.highlights_url) {
      actions.append(element("a", { className: "download-link", attributes: { href: detail.highlights_url, target: "_blank", rel: "noopener" } }, "打开看点页"));
    }
    if (detail.current_revision_status === "reviewing") {
      actions.append(element("a", { className: "download-link", attributes: { href: `#/episodes/${detail.episode.id}` } }, "查看待审修订（当前播放旧版）"));
    } else if (["approved", "delivered"].includes(detail.current_revision_status)) {
      actions.append(actionButton("再次编辑", "secondary", async () => {
        const scope = `revise:${detail.current_revision_id}`;
        await mutation(`/api/episodes/${detail.episode.id}/revisions`, scope, {
          baseRevisionId: detail.current_revision_id,
        });
        succeeded(scope);
        location.hash = `#/episodes/${detail.episode.id}`;
      }));
    }

    const parts = [heading, actions];
    if (detail.current_revision_status === "reviewing") {
      parts.push(element("p", { className: "notice warning" }, "当前展示的是已发布旧版；新修订审核并交付完成后才会替换。"));
    }
    const highlights = element("section", { className: "panel highlights-panel" });
    highlights.append(element("h2", {}, "本期看点"), element("p", {}, detail.highlights.summary));
    const items = element("ul", { className: "highlight-list" });
    for (const item of detail.highlights.items) {
      const row = element("li", {});
      const time = element("button", { className: "time-button", attributes: { type: "button" } }, formatTime(item.timestampMs));
      time.addEventListener("click", () => {
        audio.currentTime = item.timestampMs / 1000;
        audio.play().catch(() => {});
      });
      row.append(time, element("span", {}, item.text));
      items.append(row);
    }
    highlights.append(items);
    parts.push(highlights);

    const sourceToggle = element("label", { className: "source-toggle" });
    const checkbox = element("input", { attributes: { type: "checkbox" } });
    sourceToggle.append(checkbox, element("span", {}, "显示英文原文"));
    parts.push(sourceToggle);
    const cueList = element("section", { className: "cue-list hide-source", attributes: { "aria-label": "字幕" } });
    checkbox.addEventListener("change", () => cueList.classList.toggle("hide-source", !checkbox.checked));
    const cueElements = [];
    for (const cue of detail.cues) {
      const card = element("article", { className: "cue" });
      const time = element("button", { className: "time-button", attributes: { type: "button" } }, formatTime(cue.start_ms));
      time.addEventListener("click", () => {
        audio.currentTime = cue.start_ms / 1000;
        audio.play().catch(() => {});
      });
      card.append(
        time,
        element("p", { className: "source-text" }, cue.source_text),
        element("p", { className: "target-text" }, cue.text),
      );
      cueList.append(card);
      cueElements.push(card);
    }
    if (playerCleanup) playerCleanup();
    const player = window.PodcastPlayer.mount(audio, detail.cues, cueElements);
    playerCleanup = player.destroy;
    parts.push(cueList);
    parts.push(element("p", { className: "muted" }, `签名资源有效期 ${detail.expires_seconds} 秒；刷新本页可重新获取，不会重新上传。`));
    root.replaceChildren(...parts);
  }

  async function loadLibraryEpisode(episodeId) {
    renderLoading();
    try {
      const detail = await api(`/api/library/episodes/${episodeId}`);
      renderPublishedEpisode(detail);
    } catch (error) {
      handleError(error);
      root.replaceChildren(element("p", { className: "empty" }, "已发布节目加载失败。"));
    }
  }

  async function renderInbox() {
    if (playerCleanup) playerCleanup();
    playerCleanup = null;
    currentDetail = null;
    editing = null;
    renderLoading();
    try {
      const result = await api("/api/review/episodes");
      const heading = element("div", { className: "section-heading" });
      heading.append(
        element("h1", {}, "待审核节目"),
        element("span", { className: "muted" }, `${result.count} 期`),
      );
      const list = element("div", { className: "episode-list" });
      if (result.items.length === 0) {
        list.append(element("p", { className: "empty" }, "当前没有待审核节目。"));
      }
      for (const episode of result.items) {
        const link = element("a", {
          className: "episode-card",
          attributes: { href: `#/episodes/${episode.id}` },
        });
        const body = element("div", { className: "episode-card-body" });
        body.append(
          element("h2", {}, episode.title || episode.guid),
          element("p", { className: "muted" }, `${episode.feed} · ${formatDate(episode.published_at)}`),
        );
        const meta = element("div", { className: "badges" });
        meta.append(element("span", { className: "badge" }, episode.status));
        if (episode.has_conflicts) meta.append(element("span", { className: "badge danger" }, "有冲突"));
        if (episode.auto_approve_paused) {
          meta.append(element("span", { className: "badge" }, "已保留待审"));
        } else if (episode.auto_approve_at) {
          meta.append(element("span", { className: "badge" }, `自动审批：${formatDate(episode.auto_approve_at)}`));
        }
        link.append(body, meta);
        list.append(link);
      }
      root.replaceChildren(heading, list);
    } catch (error) {
      handleError(error);
      root.replaceChildren(element("p", { className: "empty" }, "审核箱加载失败。"));
    }
  }

  function actionButton(label, className, handler) {
    const button = element("button", { className: className || "secondary", attributes: { type: "button" } }, label);
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await handler();
      } catch (error) {
        handleError(error);
      } finally {
        button.disabled = false;
      }
    });
    return button;
  }

  function renderConflictPanel(detail) {
    if (!detail.has_conflicts) return null;
    const panel = element("section", { className: "panel conflict-panel" });
    panel.append(element("h2", {}, "需要处理的 override 冲突"));
    for (const conflict of detail.conflicts) {
      panel.append(element(
        "p",
        {},
        `修改 ${conflict.override_id}：${conflict.reason}，时间 ${formatTime(conflict.target_start_ms)}–${formatTime(conflict.target_end_ms)}`,
      ));
    }
    panel.append(element("p", { className: "muted" }, "冲突存在时人工与自动审批都会被阻止。"));
    return panel;
  }

  function renderEditor(cue) {
    const panel = element("section", { className: "panel editor-panel" });
    panel.append(element("h2", {}, `修改第 ${cue.entry_number} 条`));
    const source = element("p", { className: "source-text" }, cue.source_text);
    const textarea = element("textarea", { attributes: { rows: "4", maxlength: "5000" } });
    textarea.value = cue.text;
    textarea.addEventListener("input", () => { editing.dirty = true; });
    const save = actionButton("保存修改", "primary", async () => {
      const scope = `override:${currentDetail.revision_id}:${cue.entry_number}`;
      const result = await mutation(`/api/episodes/${currentDetail.episode_id}/overrides`, scope, {
        baseRevisionId: currentDetail.revision_id,
        baseEntryNumber: cue.entry_number,
        sourceArtifactId: cue.source_artifact_id,
        startMs: cue.start_ms,
        endMs: cue.end_ms,
        sourceText: cue.source_text,
        afterText: textarea.value,
      });
      succeeded(scope);
      editing = null;
      showNotice("修改已保存。", "success");
      await loadEpisode(currentDetail.episode_id);
    });
    const cancel = element("button", { className: "secondary", attributes: { type: "button" } }, "取消");
    cancel.addEventListener("click", () => {
      if (editing?.dirty && !window.confirm("放弃未保存的修改吗？")) return;
      editing = null;
      loadEpisode(currentDetail.episode_id);
    });
    const actions = element("div", { className: "actions" });
    actions.append(save, cancel);
    panel.append(source, textarea, actions);
    return panel;
  }

  function renderRejectForm(detail) {
    const panel = element("details", { className: "panel" });
    panel.append(element("summary", {}, "按时间范围驳回重跑"));
    const reason = element("input", { attributes: { type: "text", maxlength: "2000", placeholder: "驳回原因" } });
    const start = element("input", { attributes: { type: "number", min: "0", step: "0.001", value: "1" } });
    const end = element("input", { attributes: { type: "number", min: "0.001", step: "0.001", value: "3" } });
    const labels = element("div", { className: "range-fields" });
    labels.append(
      element("label", {}, "开始秒"), start,
      element("label", {}, "结束秒"), end,
    );
    const submit = actionButton("提交驳回", "danger-button", async () => {
      const scope = `reject:${detail.revision_id}`;
      await mutation(`/api/episodes/${detail.episode_id}/reject`, scope, {
        baseRevisionId: detail.revision_id,
        reason: reason.value,
        ranges: [{ startMs: Math.round(Number(start.value) * 1000), endMs: Math.round(Number(end.value) * 1000) }],
      });
      succeeded(scope);
      showNotice("驳回请求已保存，等待执行层重跑。", "success");
      await loadEpisode(detail.episode_id);
    });
    panel.append(reason, labels, submit);
    return panel;
  }

  function renderEpisode(detail) {
    currentDetail = detail;
    const heading = element("div", { className: "episode-heading" });
    const title = element("div", {});
    title.append(
      element("h1", {}, detail.episode.title || detail.episode.guid),
      element("p", { className: "muted" }, `${detail.episode.feed} · ${formatDate(detail.episode.published_at)}`),
    );
    heading.append(title, element("span", { className: "badge" }, detail.status));

    const audio = mountPlayback(
      `/api/episodes/${detail.episode_id}/audio`,
      detail.episode.title || detail.episode.guid,
    );

    const status = element("section", { className: "panel status-panel" });
    const statusText = detail.auto_approve_paused
      ? "已保留待审，不会自动审批。"
      : detail.auto_approve_at
        ? `自动审批时间：${formatDate(detail.auto_approve_at)}`
        : `当前状态：${detail.status}`;
    status.append(element("p", {}, statusText));

    const actions = element("div", { className: "actions" });
    if (detail.status === "reviewing") {
      actions.append(
        actionButton("通过并交付", "primary", async () => {
          const scope = `approve:${detail.revision_id}`;
          await mutation(`/api/episodes/${detail.episode_id}/approve`, scope, { baseRevisionId: detail.revision_id });
          succeeded(scope);
          showNotice("审批通过，等待交付。", "success");
          await loadEpisode(detail.episode_id);
        }),
        detail.auto_approve_paused
          ? actionButton("恢复 12 小时倒计时", "secondary", async () => {
              const scope = `resume:${detail.revision_id}`;
              await mutation(`/api/episodes/${detail.episode_id}/resume`, scope, { baseRevisionId: detail.revision_id });
              succeeded(scope);
              await loadEpisode(detail.episode_id);
            })
          : actionButton("保留待审", "secondary", async () => {
              const scope = `hold:${detail.revision_id}`;
              await mutation(`/api/episodes/${detail.episode_id}/hold`, scope, { baseRevisionId: detail.revision_id });
              succeeded(scope);
              await loadEpisode(detail.episode_id);
            }),
      );
    }
    if (["approved", "delivered"].includes(detail.status)) {
      actions.append(actionButton("再次编辑", "secondary", async () => {
        const scope = `revise:${detail.revision_id}`;
        await mutation(`/api/episodes/${detail.episode_id}/revisions`, scope, { baseRevisionId: detail.revision_id });
        succeeded(scope);
        await loadEpisode(detail.episode_id);
      }));
    }
    status.append(actions);

    const candidatePanel = element("section", { className: "panel candidate-panel" });
    const select = element("select", { attributes: { "aria-label": "分条版本" } });
    for (const variant of ["review", "split"]) {
      const available = detail.candidates.some((candidate) => candidate.step === variant);
      const option = element("option", { attributes: { value: variant } }, variant === "review" ? "复核版" : "重切分版");
      if (!available) option.disabled = true;
      if (detail.variant === variant) option.setAttribute("selected", "");
      select.append(option);
    }
    select.value = detail.variant;
    select.disabled = detail.status !== "reviewing";
    select.addEventListener("change", async () => {
      if (editing?.dirty && !window.confirm("切换版本会放弃当前未保存的编辑，是否继续？")) {
        select.value = detail.variant;
        return;
      }
      const scope = `variant:${detail.revision_id}:${select.value}`;
      try {
        await mutation(`/api/episodes/${detail.episode_id}/variant`, scope, {
          baseRevisionId: detail.revision_id,
          variant: select.value,
        });
        succeeded(scope);
        editing = null;
        await loadEpisode(detail.episode_id);
      } catch (error) {
        handleError(error);
      }
    });
    candidatePanel.append(element("span", {}, "字幕分条版本"), select);

    const conflictPanel = renderConflictPanel(detail);
    const cueList = element("section", { className: "cue-list", attributes: { "aria-label": "双语字幕" } });
    const cueElements = [];
    for (const cue of detail.cues) {
      const card = element("article", { className: "cue", attributes: { "data-entry": String(cue.entry_number) } });
      const time = element("button", { className: "time-button", attributes: { type: "button" } }, formatTime(cue.start_ms));
      time.addEventListener("click", () => {
        audio.currentTime = cue.start_ms / 1000;
        audio.play().catch(() => {});
      });
      card.append(
        time,
        element("p", { className: "source-text" }, cue.source_text),
        element("p", { className: "target-text" }, cue.text),
      );
      if (detail.status === "reviewing") {
        card.append(actionButton("修改", "link-button", async () => {
          editing = { cue, dirty: false };
          await loadEpisode(detail.episode_id, cue.entry_number);
        }));
      }
      cueList.append(card);
      cueElements.push(card);
    }
    if (playerCleanup) playerCleanup();
    const player = window.PodcastPlayer.mount(audio, detail.cues, cueElements);
    playerCleanup = player.destroy;

    const parts = [heading, status, candidatePanel];
    if (conflictPanel) parts.push(conflictPanel);
    if (editing) parts.push(renderEditor(editing.cue));
    if (detail.status === "reviewing") parts.push(renderRejectForm(detail));
    parts.push(cueList);
    root.replaceChildren(...parts);
    if (editing) {
      const editor = document.querySelector(".editor-panel");
      editor?.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }

  async function loadEpisode(episodeId, editEntry) {
    renderLoading();
    try {
      const detail = await api(`/api/episodes/${episodeId}/review`);
      if (editEntry && detail.status === "reviewing") {
        const cue = detail.cues.find((item) => item.entry_number === editEntry);
        editing = cue ? { cue, dirty: false } : null;
      } else if (detail.status !== "reviewing") {
        editing = null;
      }
      renderEpisode(detail);
    } catch (error) {
      handleError(error);
      root.replaceChildren(element("p", { className: "empty" }, "节目详情加载失败。"));
    }
  }

  function route() {
    clearNotice();
    const reviewMatch = /^#\/episodes\/(\d+)$/.exec(location.hash);
    const libraryMatch = /^#\/library\/episodes\/(\d+)$/.exec(location.hash);
    if (reviewMatch) {
      loadEpisode(Number(reviewMatch[1]));
    } else if (libraryMatch) {
      loadLibraryEpisode(Number(libraryMatch[1]));
    } else if (location.hash === "#/library") {
      renderLibrary();
    } else if (location.hash === "#/usage") {
      loadUsage();
    } else if (location.hash === "#/review") {
      renderInbox();
    } else {
      location.hash = "#/review";
    }
  }

  window.addEventListener("hashchange", route);
  window.addEventListener("beforeunload", (event) => {
    if (editing?.dirty) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  route();
})();
