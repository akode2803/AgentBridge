/* View registry. View modules register their entry points here at import
   time and call EACH OTHER only through V — no view ever imports another
   view, so circular imports are structurally impossible.

   Layering rule for gui/static/js:
     util / icons / api / markdown   (leaf helpers)
       → state                      (stores)
         → csel / modal / composer / picker  (UI primitives)
           → sidebar                (below the page views)
             → chat / details / media / search / members / forward / settings / wizard
               → main               (router + boot; imports every view once)

   A module may import anything strictly below it; sideways or upward calls
   go through V. main.js asserts every expected registration at boot. */

export const V = {};

export const EXPECTED = [
  "renderChats", "renderMeshChat", "renderNewChat",
  "renderChatDetails", "renderChatMedia", "renderChatSearch",
  "showAddMembers", "showSearchMembers",
  "renderSettings", "renderSetup", "refresh", "openMsgMenu",
  "showCreateGroup", "exitGroup", "openForwardPicker", "exitSelect",
];
