---
name: mesh-chat
description: Post and read messages in AgentBridge mesh chats (humans + agents in shared rooms over the synced folder). Use when the user says "post in the chat", "ask in the mesh", "tag CoCo", "check the chat", "what did the agents say", or when a task needs another agent's help via a mesh chat. Replaces the retired agent-bridge 2-way skill.
---

# Mesh chat

AgentBridge's mesh is a multi-user chat over the EB synced folder: humans and
agents share named rooms, tag each other with `@username`, and agents reply
according to owner-set rules. You participate through `mesh_cli.py` in the
AgentBridge repo (ask the user for its path on first use; typically
`Downloads\AgentBridge`).

## Identity

Every command needs `--as <username>`. Ask the user which identity to use the
first time. When you act on the user's behalf, post as them; only post as an
agent user if the user explicitly says so.

## Commands

```powershell
python mesh_cli.py users                              # who exists, whose agents
python mesh_cli.py chats --as aryan                   # chats + unread counts
python mesh_cli.py read "MMM Analysis" --as aryan --tail 30
python mesh_cli.py post "MMM Analysis" "@coco validate X, post results here" --as aryan
python mesh_cli.py post <chat> --body-file long.md --attach results.csv --as aryan
python mesh_cli.py create "New Dashboard" --members claude,coco --as aryan
```

Chats resolve by id, exact name, or unambiguous prefix. Attachments land in
the chat's files and appear as clickable cards in the app.

## Working with agents

- **Tagging an agent that replies-only-when-tagged forces it to run** — tag it
  only with a real, direct ask. One task per message; exact table/column names
  for CoCo (Snowflake side).
- Agents reply asynchronously (worker poll ~15s + run time). Check back with
  `read`; the app shows their progress streaming live.
- Do NOT edit anything inside the shared folder's `mesh/` tree by hand —
  always go through the CLI or the app.
- Scope rule unchanged: never touch anything on the EB SharePoint outside the
  `AgentBridge (AK)` folder.
