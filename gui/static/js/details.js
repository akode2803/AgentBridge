/* Chat info pane (WhatsApp "Group info" pattern) and the per-chat agents
   page. Subviews (search / media / agents) render into the same pane. */

import { $, esc, fmtTime, toast, clampLong, paneCoversChat, closeMenus } from "./util.js";
import { ICONS } from "./icons.js";
import { api, bindOpenFile } from "./api.js";
import { md } from "./markdown.js";
import { csel, mountCsels } from "./csel.js";
import { confirmModal, openPhotoViewer, openModal, closeModal } from "./modal.js";
import { App, Mesh, RULE_LABELS, meshDn, dmOther, chatDisplay, isDmLike, meshAvatarInner, meshChatAvatarInner, meshIsAdmin, chatAdmins } from "./state.js";
import { mediaThumb } from "./files.js";
import { V } from "./views.js";

// group permissions (D12 multi-admin; WhatsApp "Group permissions" minus the
// invite link). Level rows are All/Admins; the rest are on/off. Visible to
// everyone — the controls are just disabled for non-admins (per the spec).
const PERM_LEVELS = [
  ["edit_settings", "Edit group info"],
  ["send_messages", "Send messages"],
  ["add_members", "Add members"],
];
const PERM_FLAGS = [
  ["send_history", "New members see chat history"],
  ["approve_members", "Approve new members"],
  ["agents_add_if_owner_admin", "Agents can add members (when their owner is an admin)"],
  ["agents_add_if_members_can", "Agents can add members (when members can)"],
];

function permissionsCard(meta, isAdmin) {
  const p = meta.permissions || {};
  const dis = isAdmin ? "" : "disabled";
  return `
    <div class="card" id="perm-card">
      <h2>Group permissions</h2>
      <dl class="kv" style="grid-template-columns:1fr minmax(120px,150px);align-items:center">
        ${PERM_LEVELS.map(([k, label]) =>
          `<dt>${label}</dt><dd><span class="csel-slot perm-lvl" data-field="${k}"
            data-value="${esc(p[k] || "all")}" data-admin="${isAdmin ? 1 : 0}"></span></dd>`).join("")}
      </dl>
      ${PERM_FLAGS.map(([k, label]) => `
        <div class="row" style="margin-top:8px"><label class="switch">
          <input type="checkbox" class="perm-flag" data-field="${k}" ${p[k] ? "checked" : ""} ${dis}>
          <span class="slider"></span></label><span>${label}</span></div>`).join("")}
      ${isAdmin ? "" : '<p class="hint" style="margin-bottom:0">Only admins can change these.</p>'}
    </div>`;
}

// agent reply-rule + model dropdowns (the per-chat agents page). Every change
// writes immediately; picking "Default" writes null, which clears the per-chat
// entry (rules/models merge server-side — other chats' picks survive).
function mountAgentSlots(scope, chatId, fams) {
  mountCsels(scope, (slot) => {
    if (slot.classList.contains("cd-model")) {
      const fam = fams.find((f) => f.id === slot.dataset.fam);
      return [
        { v: "", label: `Default — ${slot.dataset.def || "family default"}` },
        ...((fam && fam.models) || []).map((m) => ({ v: m, label: m })),
      ];
    }
    const def = RULE_LABELS[slot.dataset.def] || "";
    return [{ v: "", label: `Default — ${def.toLowerCase()}` },
      ...Object.entries(RULE_LABELS).map(([v, label]) => ({ v, label }))];
  }, async (slot, v) => {
    const key = slot.classList.contains("cd-model") ? "models" : "rules";
    const r = await api("/api/mesh/agent", {
      username: slot.dataset.agent, patch: { [key]: { [chatId]: v || null } },
    });
    if (r.error) toast(r.error, true);
  });
}

async function renderChatDetails() {
  const ms = Mesh.state;
  const chatId = Mesh.chatId;
  // an open inline edit (name/description) survives polls — it only closes
  // when saved or when the pane goes away. `.ci-saving` is the brief
  // committing state (spinner in place of the ✓): hold the pane there too so a
  // poll doesn't flash the pre-write name back in (round 11).
  if (document.querySelector("#ci-name-input, #ci-desc-input, .ci-saving")) return;
  // chat_info is the LIGHT payload (meta + files + links) — the pane used
  // to pull 1000 full messages on every open and poll
  const data = await api(`/api/mesh/chat_info?id=${encodeURIComponent(chatId)}`);
  if (data.error) {
    if (data.error !== "No such chat") toast(data.error, true);   // deleted → quiet
    location.hash = "#/chats"; return;
  }
  const meta = data.meta;
  // "can I administer this group" — v2 multi-admin, v1 single-owner (adapter)
  const isOwner = meshIsAdmin(meta);
  const admins = chatAdmins(meta);
  const media = data.files || [];
  const myAgentsHere = Object.values(ms.users).filter((u) =>
    u.kind === "agent" && (u.owners || []).includes(ms.user)
    && (meta.members || []).includes(u.username));
  // only re-render when something actually changed — a poll redraw would
  // knock dropdowns and toggles out from under the user
  const dKey = JSON.stringify([meta, media.length, (data.links || []).length,
    (data.starred || []).length, myAgentsHere.map((a) => a.settings),
    !!Mesh.searchView, !!Mesh.mediaView, Mesh.mediaTab, !!Mesh.agentsView,
    !!Mesh.starredPane]);
  if (dKey === Mesh.detailsKey && App.page === "chats") return;
  Mesh.detailsKey = dKey;

  // search / media / starred / agents pages slide in over chat info
  if (Mesh.searchView) return V.renderChatSearch();
  if (Mesh.mediaView) return V.renderChatMedia(data);
  if (Mesh.starredPane) return renderChatStarred(data);
  if (Mesh.agentsView) return renderChatAgents(myAgentsHere, meta);

  const isMember = (meta.members || []).includes(ms.user);
  const memberRow = (u) => {
    const rec = ms.users[u] || {};
    // an admin gets a per-member menu on everyone but themselves (promote/
    // demote other humans, remove). The mesh re-checks every action, so the
    // menu can be permissive; the fold is the real gate.
    const actionable = isOwner && u !== ms.user;
    return `
      <div class="mem-row">
        <span class="mem-avatar">${meshAvatarInner(u)}</span>
        <span style="min-width:0">
          <div class="mem-name">${esc(meshDn(u))}
            ${rec.kind === "agent" ? '<span class="kind-tag">agent</span>' : ""}</div>
          <div class="mem-sub">@${esc(u)}</div>
        </span>
        ${admins.includes(u) ? '<span class="owner-chip">Admin</span>' : ""}
        ${actionable ? `<button class="mem-chevron icon-btn" data-user="${esc(u)}"
          data-admin="${admins.includes(u) ? 1 : 0}"
          data-agent="${rec.kind === "agent" ? 1 : 0}">${ICONS.chevD}</button>` : ""}
      </div>`;
  };

  const isDm = isDmLike(meta);   // self-chat uses the DM info layout
  const isSelf = meta.kind === "self";
  const title = chatDisplay(meta, ms.user);
  const noun = isDm ? "chat" : "group";
  // me first, then admins, then everyone else
  const ordered = [...(meta.members || [])].sort((a, b) => {
    const rank = (u) => (u === ms.user ? 0 : admins.includes(u) ? 1 : 2);
    return rank(a) - rank(b);
  });
  const nMembers = (meta.members || []).length;
  const memberCount = `${nMembers} member${nMembers === 1 ? "" : "s"}`;
  $("#details-pane").innerHTML = `
    <div class="pane-head">
      <button class="icon-btn" id="cd-close">${ICONS.close}</button>
      <span class="pane-title">${isDm ? "Chat info" : "Group info"}</span>
    </div>
    <div class="ci-identity">
      <div class="ci-avatar-wrap">
        <div class="ci-avatar">${meshChatAvatarInner(meta)}</div>
        ${(!isDm && isOwner) ? `<button class="ci-cam" id="ci-photo" aria-label="Change group photo">${ICONS.camera}</button>
        <div class="menu ci-photo-menu" id="ci-photo-menu" hidden>
          <button data-act="camera">${ICONS.camera} ${meta.avatar ? "Retake photo" : "Take photo"}</button>
          <button data-act="upload">${ICONS.media} Upload photo</button>
          ${meta.avatar ? `<button class="danger-item" data-act="remove">${ICONS.trash} Remove photo</button>` : ""}
        </div>` : ""}
      </div>
      <div class="ci-name-row" id="ci-name-row">
        <span class="ci-name">${esc(title)}
          ${meta.archived ? '<span class="kind-tag">archived</span>' : ""}</span>
        ${!isDm && isOwner ? `<button class="icon-btn ci-pencil" id="ci-rename">${ICONS.pencil}</button>` : ""}
      </div>
      <div class="ci-sub">${isSelf ? "Message yourself"
        : isDm ? "@" + esc(dmOther(meta, ms.user))
        : `Group · ${memberCount}`}</div>
      <div class="ci-actions">
        ${isDm ? "" : `<button class="ci-act" id="ci-add">
          <span class="ci-act-circle">${ICONS.addUser}</span>Add</button>`}
        <button class="ci-act" id="ci-search">
          <span class="ci-act-circle">${ICONS.search}</span>Search</button>
      </div>
    </div>
    ${isDm ? "" : `
    <div class="card" id="ci-desc-wrap">
      <div class="ci-desc-row">
        <div class="ci-desc-text">${meta.description ? esc(meta.description)
          : (isOwner ? '<span class="ci-desc-add">Add group description</span>'
                     : '<span class="hint">No description</span>')}</div>
        ${isOwner ? `<button class="icon-btn ci-pencil" id="ci-desc-edit">${ICONS.pencil}</button>` : ""}
      </div>
    </div>`}
    <div class="card" style="padding-top:8px;padding-bottom:10px">
      <button class="sec-head" id="media-sec">
        ${ICONS.media}<span class="sec-label">Media and files</span>
        <span class="sec-count">${media.length}</span>
      </button>
      ${media.length ? `<div class="media-strip">
        ${media.slice(-6).reverse().map((f) => `
          <button class="media-tile-btn cd-file" data-path="${esc(f.path)}">
            ${mediaThumb(chatId, f)}</button>`).join("")}
      </div>` : ""}
    </div>
    <div class="card" style="padding-top:8px;padding-bottom:8px">
      <button class="sec-head" id="starred-sec">
        ${ICONS.star}<span class="sec-label">Starred messages</span>
        <span class="sec-count">${(data.starred || []).length}</span>
      </button>
    </div>
    ${myAgentsHere.length ? `
    <div class="card" style="padding-top:8px;padding-bottom:8px">
      <button class="sec-head" id="agents-sec">
        ${ICONS.bot}<span class="sec-label">Your agents in this ${noun}</span>
        <span class="sec-count">${myAgentsHere.length}</span>
      </button>
    </div>` : ""}
    ${(() => {
      // R31: the Encryption card (DM info, WhatsApp pattern) — the peer's
      // trusted-key fingerprint + the out-of-band "mark verified" action.
      // Both devices derive the same code from the same (name, keys), so
      // comparing it over a call/in person closes the first-contact gap.
      if (!isDm || isSelf || !ms.encrypted) return "";
      const peer = ms.users[dmOther(meta, ms.user)] || {};
      if (!peer.key_fp) return "";
      return `
    <div class="card" id="enc-card">
      <div class="sec-head" style="cursor:default">
        ${ICONS.key}<span class="sec-label">Encryption</span>
        ${peer.key_verified ? '<span class="owner-chip">Verified</span>' : ""}
      </div>
      <p class="hint" style="margin:6px 0 8px">Messages are end-to-end
        encrypted. Compare this code with @${esc(peer.username || "")} over a
        call or in person — if it matches on both devices, you're talking to
        the right keys.</p>
      <code class="key-fp-code">${esc(peer.key_fp)}</code>
      ${peer.key_verified ? "" : `<button id="enc-verify" class="primary"
        style="margin-top:8px">Mark as verified</button>`}
    </div>`;
    })()}
    ${isDm ? "" : `
    <div class="card">
      <div class="mem-head">
        <span>${memberCount}</span>
        <button class="icon-btn" id="mem-search">${ICONS.search}</button>
      </div>
      ${isMember ? `<button class="mem-add" id="ci-add2">
        <span class="mem-avatar">${ICONS.addUser}</span>
        <span style="min-width:0"><div class="mem-name">Add member</div></span>
      </button>` : ""}
      ${ordered.map(memberRow).join("")}
    </div>`}
    ${isDm ? "" : permissionsCard(meta, isOwner)}
    <div class="card danger-card">
      ${isOwner ? `<button class="danger-row neutral" id="dg-archive">
        ${ICONS.archive} ${meta.archived ? `Unarchive ${noun}` : `Archive ${noun}`}</button>` : ""}
      ${isMember && !isOwner && !isDm ? `<button class="danger-row" id="dg-exit">
        ${ICONS.exit} Exit group</button>` : ""}
      ${isOwner ? `<button class="danger-row" id="dg-delete">
        ${ICONS.trash} Delete ${noun}</button>` : ""}
    </div>
    ${isDm ? "" : `<div class="ci-footer">Group created by ${
      esc(meshDn(meta.created_by))}, ${esc(fmtTime(meta.created))}</div>`}`;

  $("#cd-close").addEventListener("click", () => { location.hash = `#/chats/${chatId}`; });
  const encVerify = $("#enc-verify");
  if (encVerify) encVerify.addEventListener("click", async () => {
    if (await markKeyVerified(dmOther(meta, ms.user))) {
      Mesh.detailsKey = "";
      renderChatDetails();
    }
  });
  $("#ci-search").addEventListener("click", () => {
    Mesh.searchView = true;
    Mesh.detailsKey = "";
    renderChatDetails();
  });
  const ciAdd = $("#ci-add");
  if (ciAdd) ciAdd.addEventListener("click", () => V.showAddMembers(chatId));
  const add2 = $("#ci-add2");
  if (add2) add2.addEventListener("click", () => V.showAddMembers(chatId));
  const memSearch = $("#mem-search");
  if (memSearch) memSearch.addEventListener("click", () => V.showSearchMembers(chatId));
  $("#media-sec").addEventListener("click", () => {
    Mesh.mediaView = true;
    Mesh.mediaTab = Mesh.mediaTab || "media";
    Mesh.detailsKey = "";
    renderChatDetails();
  });
  const agentsSec = $("#agents-sec");
  if (agentsSec) agentsSec.addEventListener("click", () => {
    Mesh.agentsView = true;
    Mesh.detailsKey = "";
    renderChatDetails();
  });
  $("#starred-sec").addEventListener("click", () => {
    Mesh.starredPane = true;
    Mesh.detailsKey = "";
    renderChatDetails();
  });
  // thumbnails open their file directly (the section header opens the
  // browser) — bound ONCE; the old separate .media-tile-btn/.cd-file
  // binds stacked two listeners and opened every file twice
  bindOpenFile($("#details-pane"), chatId, ".cd-file");
  // click the chat photo → open the viewer, but ONLY when there's actually a
  // photo (the hand cursor + click are gated on the rendered <img>) — task 6
  const ciAva = $(".ci-identity .ci-avatar");
  const ciAvaImg = ciAva?.querySelector(".avatar-img");
  if (ciAva && ciAvaImg) {
    ciAva.classList.add("viewable");
    ciAva.addEventListener("click", () => openPhotoViewer(ciAvaImg.src, title, ciAva));
  }
  // owner: change the group photo — Take photo / Upload photo / Remove, using
  // the shared capture flows registered by settings.js on V
  const ciPhoto = $("#ci-photo");
  if (ciPhoto) {
    const pmenu = $("#ci-photo-menu");
    ciPhoto.addEventListener("click", (e) => {
      e.stopPropagation();
      const opening = pmenu.hidden;
      pmenu.hidden = !opening;
      if (opening) {   // dismiss on the next outside click
        const closer = (ev) => {
          if (!pmenu.contains(ev.target) && !ciPhoto.contains(ev.target)) {
            pmenu.hidden = true;
            document.removeEventListener("mousedown", closer);
          }
        };
        setTimeout(() => document.addEventListener("mousedown", closer), 0);
      }
    });
    pmenu.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
      pmenu.hidden = true;
      const onBlob = (blob) => uploadGroupAvatar(chatId, blob);
      if (b.dataset.act === "camera") V.photoCamera(onBlob);
      else if (b.dataset.act === "upload") V.photoPickFile(onBlob);
      else if (b.dataset.act === "remove") clearGroupAvatar(chatId);
    }));
  }

  // in-place edits (WhatsApp pattern): the pencil swaps just that row for
  // an input + ✓; it stays open until saved or the pane goes away
  const rename = $("#ci-rename");
  if (rename) rename.addEventListener("click", () => {
    $("#ci-name-row").innerHTML = `
      <input type="text" id="ci-name-input" class="ci-edit" maxlength="60"
             value="${esc(meta.name)}">
      <button class="icon-btn ci-ok" id="ci-name-save">${ICONS.check}</button>`;
    const inp = $("#ci-name-input");
    inp.focus();
    inp.setSelectionRange(inp.value.length, inp.value.length);
    const save = async () => {
      const name = inp.value.trim();
      if (!name || name === meta.name.trim()) {   // unchanged / empty: just close
        inp.remove();
        Mesh.detailsKey = "";
        renderChatDetails();
        return;
      }
      // keep the NEW name in place and swap the ✓ for a spinner while the
      // write commits (the shared folder can lag) — the row never flickers to
      // empty, and the `.ci-saving` guard freezes polls until we redraw.
      $("#ci-name-row").innerHTML = `
        <span class="ci-name">${esc(name)}</span>
        <span class="ci-ok ci-saving" style="width:34px;height:34px;display:grid;place-items:center">
          <span class="spin-sm"></span></span>`;
      const r = await api("/api/mesh/rename", { chat_id: chatId, name });
      if (r.error) toast(r.error, true);
      // surgical: patch the header + sidebar row + structKey in place instead
      // of a full renderChats (which rebuilt the transcript + swapped the whole
      // sidebar = the stutter). Only the info pane repaints, once (round 12).
      else { meta.name = r.name || name; V.patchChatName(chatId, meta.name); }
      Mesh.detailsKey = "";
      document.querySelector("#ci-name-row .ci-saving")?.remove();
      renderChatDetails();
    };
    $("#ci-name-save").addEventListener("click", save);
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") save(); });
  });
  const descEdit = $("#ci-desc-edit");
  if (descEdit) descEdit.addEventListener("click", () => {
    $("#ci-desc-wrap").innerHTML = `
      <div class="ci-desc-row">
        <input type="text" id="ci-desc-input" class="ci-edit" maxlength="300"
          placeholder="What is this group for?" value="${esc(meta.description || "")}">
        <button class="icon-btn ci-ok" id="ci-desc-save">${ICONS.check}</button>
      </div>`;
    const inp = $("#ci-desc-input");
    inp.focus();
    inp.setSelectionRange(inp.value.length, inp.value.length);
    const save = async () => {
      const description = inp.value.trim();
      if (description === (meta.description || "").trim()) {   // unchanged: close
        inp.remove();
        Mesh.detailsKey = "";
        renderChatDetails();
        return;
      }
      // keep the new text in place + spinner while the write commits (round 11)
      $("#ci-desc-wrap").innerHTML = `
        <div class="ci-desc-row">
          <div class="ci-desc-text">${description ? esc(description)
            : '<span class="ci-desc-add">Add group description</span>'}</div>
          <span class="ci-ok ci-saving" style="width:34px;height:34px;display:grid;place-items:center">
            <span class="spin-sm"></span></span>
        </div>`;
      const r = await api("/api/mesh/set_description",
        { chat_id: chatId, description });
      if (r.error) toast(r.error, true);
      Mesh.detailsKey = "";
      // see rename: clear the committing marker so the guard lets us repaint
      document.querySelector("#ci-desc-wrap .ci-saving")?.remove();
      renderChatDetails();
    };
    $("#ci-desc-save").addEventListener("click", save);
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") save(); });
  });
  // owner-only remove: chevron appears on hover, opens a small menu
  document.querySelectorAll(".mem-chevron").forEach((b) => {
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      closeMenus();
      const user = b.dataset.user;
      const isAdminMember = b.dataset.admin === "1";
      const isAgent = b.dataset.agent === "1";
      const row = b.closest(".mem-row");
      const menu = document.createElement("div");
      menu.className = "menu mem-menu";
      // agents can never be admins (D12) — no promote row for them
      const adminRow = isAgent ? ""
        : isAdminMember
          ? `<button data-act="revoke">${ICONS.close} Dismiss as admin</button>`
          : `<button data-act="grant">${ICONS.check} Make admin</button>`;
      menu.innerHTML = `${adminRow}
        <button class="danger-item" data-act="remove">${ICONS.close} Remove @${esc(user)}</button>`;
      row.appendChild(menu);
      const run = async (path) => {
        menu.remove();
        const r = await api(path, { chat_id: chatId, username: user });
        if (r.error) { toast(r.error, true); return; }
        Mesh.detailsKey = ""; Mesh.structKey = "";
        renderChatDetails(); V.renderChats(true);
      };
      menu.querySelectorAll("button").forEach((btn) => {
        btn.addEventListener("click", () => {
          const act = btn.dataset.act;
          run(act === "grant" ? "/api/mesh/grant_admin"
            : act === "revoke" ? "/api/mesh/revoke_admin"
            : "/api/mesh/remove_member");
        });
      });
      document.addEventListener("mousedown", function away(ev) {
        if (!menu.contains(ev.target)) {
          menu.remove();
          document.removeEventListener("mousedown", away);
        }
      });
    });
  });
  // group permissions: level selects + flag switches (admins only; the mesh
  // re-checks). Each change POSTs a single-field set_permissions patch.
  document.querySelectorAll(".perm-lvl").forEach((slot) => {
    const isAdmin = slot.dataset.admin === "1";
    slot.appendChild(csel({
      options: [{ v: "all", label: "Everyone" }, { v: "admins", label: "Admins only" }],
      value: slot.dataset.value || "all",
      disabled: !isAdmin,
      onChange: async (v) => {
        const r = await api("/api/mesh/set_permissions",
          { chat_id: chatId, permissions: { [slot.dataset.field]: v } });
        if (r.error) { toast(r.error, true); return; }
        Mesh.detailsKey = "";
      },
    }));
  });
  document.querySelectorAll(".perm-flag").forEach((cb) => {
    cb.addEventListener("change", async (e) => {
      const r = await api("/api/mesh/set_permissions",
        { chat_id: chatId, permissions: { [cb.dataset.field]: e.target.checked } });
      if (r.error) { toast(r.error, true); e.target.checked = !e.target.checked; return; }
      Mesh.detailsKey = "";
    });
  });
  const dgArch = $("#dg-archive");
  if (dgArch) dgArch.addEventListener("click", async () => {
    const r = await api("/api/mesh/archive", { chat_id: chatId, archived: !meta.archived });
    if (r.error) { toast(r.error, true); return; }
    location.hash = "#/chats";
  });
  const dgExit = $("#dg-exit");
  if (dgExit) dgExit.addEventListener("click", () => exitGroup(chatId, title));
  const dgDel = $("#dg-delete");
  if (dgDel) dgDel.addEventListener("click", async () => {
    if (!await confirmModal({
      title: isSelf ? "Delete this chat?"
        : `Delete ${isDm ? "chat with" : ""} ${esc(title)}?`,
      body: "Messages and files will be removed for everyone. " +
            "Archiving keeps them instead.",
      action: "Delete",
    })) return;
    const r = await api("/api/mesh/delete_chat", { chat_id: chatId });
    if (r.error) { toast(r.error, true); return; }
    location.hash = "#/chats";
  });
}
V.renderChatDetails = renderChatDetails;

// R32: the ONE verify mutation, shared by the info-pane Encryption card and
// the transcript pill's dialog. Patches the CURRENT state object (a poll may
// have replaced Mesh.state since the caller's render captured `ms`). Returns
// whether it took.
async function markKeyVerified(name) {
  const r = await api("/api/mesh/key_verify", { name });
  if (r.error) { toast(r.error, true); return false; }
  const cur = Mesh.state;
  if (cur?.users?.[name]) cur.users[name].key_verified = r.verified || "now";
  toast(`@${name}'s keys marked verified`, { check: true });
  return true;
}

// R32: the focused key-verification dialog — the transcript's E2EE pill nudge
// opens THIS instead of the info pane, so the fingerprint + action are right
// under the cursor (the card sits below the fold in chat info). Same code,
// same endpoint as the card; a modal is just the direct surface.
function openKeyVerify(name) {
  const rec = Mesh.state?.users?.[name] || {};
  if (!rec.key_fp) { toast("No key to verify yet", true); return; }
  const verified = !!rec.key_verified;
  const box = openModal(`
    <div class="cf-title">Verify @${esc(name)}'s keys</div>
    <div class="cf-body">Compare this code with the one @${esc(name)} sees
      (their chat info, or Settings &rarr; Account) over a call or in person.
      If it matches on both devices, no one has swapped the keys.</div>
    <code class="key-fp-code" style="margin:4px 0 2px">${esc(rec.key_fp)}</code>
    <div class="cf-actions">
      ${verified
        ? `<span class="owner-chip">Verified</span>
           <button class="cf-cancel" id="kv-x">Close</button>`
        : `<button class="cf-cancel" id="kv-x">Not now</button>
           <button class="cf-go" id="kv-go">Mark as verified</button>`}
    </div>`);
  box.classList.add("confirm");
  box.parentElement.classList.add("confirm-scrim");
  box.querySelector("#kv-x").addEventListener("click", closeModal);
  const go = box.querySelector("#kv-go");
  if (go) go.addEventListener("click", async () => {
    const ok = await markKeyVerified(name);
    closeModal();
    if (ok) {   // the pill nudge + info pane both key off key_verified
      if (Mesh.chatId) { Mesh.structKey = ""; V.renderMeshChat(true); }
      if (Mesh.detailsView) { Mesh.detailsKey = ""; renderChatDetails(); }
    }
  });
}
V.openKeyVerify = openKeyVerify;

// group photo (owner-only) — POST the finished 512px JPEG blob, then repaint
// the pane; the header (if the chat is open) and the sidebar row pick up the
// new marker via Mesh.state + a forced structKey.
async function uploadGroupAvatar(chatId, blob) {
  if (!blob) { toast("Couldn't process that image", true); return; }
  toast("Setting group photo", { spinner: true });
  try {
    const r = await fetch(`/api/mesh/set_group_avatar?chat=${encodeURIComponent(chatId)}`,
                          { method: "POST", body: blob });
    const j = await r.json();
    if (j.error) { toast(j.error, { error: true, swap: true }); return; }
    const c = (Mesh.state?.chats || []).find((k) => k.id === chatId);
    if (c) c.avatar = j.avatar;   // reflect now, don't wait for the poll
    toast("Group photo set", { check: true, swap: true });
    afterGroupAvatar(chatId);
  } catch (e) {
    toast("Couldn't set the photo — try again", { error: true, swap: true });
  }
}

async function clearGroupAvatar(chatId) {
  const r = await api("/api/mesh/clear_group_avatar", { chat_id: chatId });
  if (r.error) { toast(r.error, true); return; }
  const c = (Mesh.state?.chats || []).find((k) => k.id === chatId);
  if (c) delete c.avatar;
  toast("Group photo removed", { check: true });
  afterGroupAvatar(chatId);
}

function afterGroupAvatar(chatId) {
  Mesh.detailsKey = "";
  renderChatDetails();   // pane re-fetches meta (carries the new marker)
  if (Mesh.chatId === chatId) { Mesh.structKey = ""; V.renderMeshChat(true); }
}

// Leaving a group = removing yourself. Shared by the chat-info danger row and
// the header ⋮ menu (chat.js), so both confirm and behave identically. The
// caller decides WHEN to show it (member, not owner, not a DM).
export async function exitGroup(chatId, title) {
  const ms = Mesh.state;
  if (!await confirmModal({
    title: `Exit "${esc(title)}"?`,
    body: "You can be added back by a member.",
    action: "Exit",
  })) return;
  const r = await api("/api/mesh/remove_member",
    { chat_id: chatId, username: ms.user });
  if (r.error) { toast(r.error, true); return; }
  location.hash = "#/chats";
}
V.exitGroup = exitGroup;

// starred messages for THIS chat (WhatsApp: a row in chat info, under
// media). Cards carry a LITERAL snapshot of the message — same markdown,
// same bubble colors, same read-more clamp as the transcript — plus the
// message context menu on right-click.
async function renderChatStarred(info) {
  const ms = Mesh.state;
  const chatId = Mesh.chatId;
  const meta = info.meta || {};
  const isDm = isDmLike(meta);
  const canReply = (meta.members || []).includes(ms.user) && !meta.archived;
  const data = await api(`/api/mesh/starred?id=${encodeURIComponent(chatId)}`);
  if (data.error) { toast(data.error, true); return; }
  const items = data.starred || [];
  const card = (s) => {
    const mine = s.from === ms.user;
    const sender = mine ? "You" : meshDn(s.from);
    const receiver = isDm
      ? (mine ? meshDn(dmOther(meta, ms.user)) : "You")
      : meta.name;
    return `
    <div class="star-card" data-mid="${esc(s.id)}">
      <span class="sc-top">
        <span class="sc-names">${esc(sender)} <span class="sc-arrow">›</span> ${esc(receiver)}</span>
        <span class="sc-time">${esc(fmtTime(s.ts))}</span>
        <span class="sc-chev">${ICONS.chevD}</span>
      </span>
      <div class="msg sc-snap ${mine ? "mine" : ""}" data-mid="${esc(s.id)}">
        <div class="bubble">
          <button class="msg-arrow" aria-label="Message menu">${ICONS.chevD}</button>
          <div class="msg-body">${md(s.body || "")}</div></div>
      </div>
    </div>`;
  };
  $("#details-pane").innerHTML = `
    <div class="pane-head">
      <button class="icon-btn" id="cst-back">${ICONS.back}</button>
      <span class="pane-title">Starred messages</span>
    </div>
    <div class="pane-view">
      <div class="search-box" style="margin:0 0 4px">${ICONS.search}
        <input type="text" id="cst-q" placeholder="Search" autocomplete="off">
      </div>
      <div id="cst-list">${items.map(card).join("") ||
        `<div class="empty" style="padding:26px 0">Nothing starred in this chat</div>`}</div>
    </div>`;
  $("#cst-back").addEventListener("click", () => {
    Mesh.starredPane = false;
    Mesh.detailsKey = "";
    renderChatDetails();
  });
  $("#cst-q").addEventListener("input", (e) => {
    const q = e.target.value.trim().toLowerCase();
    document.querySelectorAll("#cst-list .star-card").forEach((c) => {
      c.hidden = !!q && !c.textContent.toLowerCase().includes(q);
    });
  });
  const list = $("#cst-list");
  // the pane has its OWN expansion state: snapshots always open in the
  // default collapsed view, whatever was expanded in the transcript
  const expand = {};
  clampLong(list, expand);
  const bySig = new Map(items.map((s) => [s.id, s]));
  // below 1100px the pane COVERS the chat — anything that needs the chat
  // (jump, reply) closes the pane first; beside the chat it stays open
  const menuCtx = { isDm, canReply, pins: [], fromPane: true };
  const openCardMenu = (rect, s) => V.openMsgMenu(rect,
    { id: s.id, from: s.from, body: s.body, ts: s.ts, mine: s.from === ms.user },
    chatId, { ...menuCtx, starred: new Set([s.id]) });
  list.addEventListener("click", (e) => {
    const rm = e.target.closest(".read-more");
    if (rm) {
      const mid = rm.closest("[data-mid]")?.dataset.mid;
      expand[mid] = (expand[mid] || 10) + 10;
      clampLong(rm.closest(".star-card"), expand);
      return;
    }
    const ar = e.target.closest(".msg-arrow");
    if (ar) {
      const s = bySig.get(ar.closest(".star-card")?.dataset.mid);
      if (!s) return;
      let rect = ar.getBoundingClientRect();
      if (!rect.width) rect = ar.closest(".bubble").getBoundingClientRect();
      openCardMenu(rect, s);
      return;
    }
    const c = e.target.closest(".star-card");
    if (!c) return;
    Mesh.jumpTo = c.dataset.mid;
    if (paneCoversChat()) {
      location.hash = `#/chats/${chatId}`;   // close the pane, land on it
    } else {
      Mesh.chatKey = "";
      V.renderChats(true);                   // chat is visible beside us
    }
  });
  list.addEventListener("contextmenu", (e) => {
    const c = e.target.closest(".star-card");
    const s = c && bySig.get(c.dataset.mid);
    if (!s) return;
    e.preventDefault();
    openCardMenu({ left: e.clientX, right: e.clientX,
                   top: e.clientY, bottom: e.clientY }, s);
  });
}

// per-chat agent rules + models — its own page off chat info (a full
// permissions overhaul comes later)
async function renderChatAgents(agents, meta) {
  const chatId = Mesh.chatId;
  const isDm = isDmLike(meta || {});
  // reached from the composer's hand → a Close that dismisses the pane;
  // reached from chat info → a Back that returns to it
  const fromComposer = Mesh.agentsFromComposer;
  // model options come from this machine's preset catalog (the same source
  // as Settings → My agents); a family with no model list gets no model row
  const ho = await api("/api/mesh/harness_options");
  const FAMS = (ho && ho.families) || [];
  const avail = FAMS.filter((f) => f.available);
  const famFor = (st) => FAMS.find((f) => f.id === (st.adapter || ""))
    || (avail.length === 1 ? avail[0] : null);
  $("#details-pane").innerHTML = `
    <div class="pane-head">
      <button class="icon-btn" id="ca-back">${fromComposer ? ICONS.close : ICONS.back}</button>
      <span class="pane-title">Your agents</span>
    </div>
    <div class="card pane-view" style="border-bottom:none">
      <dl class="kv" style="grid-template-columns:minmax(90px,130px) 1fr">
        ${agents.map((a) => {
          const st = a.settings || {};
          const fam = famFor(st);
          const rows = [`<dt>${esc(a.display)}</dt>
            <dd><div class="csel-slot cd-rule" data-agent="${esc(a.username)}"
                     data-value="${esc((st.rules || {})[chatId] || "")}"
                     data-def="${esc(isDm ? "all"
                       : st.default_rule || "tagged")}"></div></dd>`];
          if (fam && (fam.models || []).length) {
            rows.push(`<dt class="cd-sub">Model here</dt>
              <dd><div class="csel-slot cd-model" data-agent="${esc(a.username)}"
                       data-fam="${esc(fam.id)}"
                       data-value="${esc((st.models || {})[chatId] || "")}"
                       data-def="${esc(st.model || fam.default_model
                         || "family default")}"></div></dd>`);
          }
          return rows.join("");
        }).join("")}
      </dl>
      <p class="hint" style="margin-bottom:0">Rules and models apply from the
      agent's next check and only in this chat. Defaults live in Settings →
      My agents.</p>
    </div>`;
  $("#ca-back").addEventListener("click", () => {
    Mesh.agentsView = false;
    Mesh.detailsKey = "";
    if (fromComposer) {
      Mesh.agentsFromComposer = false;   // close the pane, back to the chat
      location.hash = `#/chats/${chatId}`;
      return;
    }
    renderChatDetails();
  });
  mountAgentSlots($("#details-pane"), chatId, FAMS);
}
