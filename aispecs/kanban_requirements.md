# Kanban backend requirements for kankan

**Status:** v1.3 — attachments and board-settings endpoints in use
**Date:** 2026-11-10
**Related:** [`initial_spec_functional.md`](initial_spec_functional.md), [`initial_spec_technical.md`](initial_spec_technical.md)

## 1. Purpose

kankan coordinates AI coding agents through a Kanban board. Its reference backend is Trello, isolated in the `trello` module. This document lists the capabilities that any other Kanban product must expose through its API to replace Trello without changing kankan's workflow. It also maps each capability to the Trello REST endpoint kankan uses today, so an adapter author can see the exact semantics required.

Requirement levels:
- **MUST**: kankan cannot work without it.
- **SHOULD**: kankan works without it, but with a degraded experience or a documented workaround.
- **MAY**: an improvement kankan could use if available.

## 2. General API properties

| ID | Level | Requirement |
|---|---|---|
| G1 | MUST | HTTP/JSON API usable from a headless server with a long-lived, per-user credential (API key + token, personal access token or equivalent). No interactive OAuth on every run. |
| G2 | MUST | Credentials can be scoped to read and write boards, lists, cards, labels and comments. |
| G3 | MUST | Every object (board, list, card, label, comment, member) has a stable, immutable ID that survives renames and moves. |
| G4 | MUST | Successful writes return the updated object, and a subsequent read returns the written state (read-your-writes consistency for the same credential). |
| G5 | MUST | Distinguishable error classes: authentication, not found, validation, rate limiting (with a retry hint such as HTTP 429 and `Retry-After`) and server errors. |
| G6 | MUST | A documented rate limit high enough for ~10 agents polling every 2 minutes plus bursts of a few dozen requests (Trello: about 100 requests per 10 seconds per token). |
| G7 | SHOULD | Writes are idempotent or can carry a client-supplied idempotency key, so a retry after a timeout cannot duplicate a comment or a label. kankan's workaround: read back before retrying and embed an operation marker in comment text. |
| G8 | MAY | Conditional updates (ETag/`If-Match`, a version number or "move only if still in list X"). Trello lacks them; with them, kankan could guarantee that a human's concurrent move is never overwritten (see [spec §14.5](initial_spec_functional.md#145-precedence-of-human-changes)). |
| G9 | MAY | Webhooks or an event stream for card moves, label changes and new comments, to replace polling. |

## 3. Boards

| ID | Level | Capability | Trello endpoint used by kankan |
|---|---|---|---|
| B1 | MUST | Resolve a board from the URL the user pastes (short code or slug) to its stable ID. | `GET /1/boards/{shortLink}` |
| B2 | MUST | Read and update a free-text board description, preserving the content outside kankan's delimited block. Used for the board↔registry binding. | read through the nested board read of B3; `PUT /1/boards/{id}` (`desc`) |
| B3 | MUST | Read in **one request** the board description, all lists, all open cards (list ID, position, labels, attachments, title, description, URL, archived state) and all labels. kankan performs this read on every launcher cycle and before every state-changing command. | `GET /1/boards/{id}?fields=name,desc&lists=all&cards=open&card_attachments=true&card_attachment_fields=url&labels=all&labels_limit=1000&...` (nested resources); `checklists=all` for the setting cards ([board settings §3](boardsettings_spec_functional.md#3-model-and-effort)) |
| B4 | MUST | Read all lists of a board with ID, name, position and archived state. | part of the B3 read (`lists=all&list_fields=name,closed,pos`); `GET /1/boards/{id}?lists=open` to find a list by name after a create that timed out |
| B5 | MUST | Read all labels defined on a board, more than 50 included. | part of the B3 read (`labels_limit=1000`); `GET /1/boards/{id}/labels?limit=1000[&before=...]` when 1000 labels come back, and to find a label by name after a create that timed out |
| B6 | MUST | Read a board's members with their role (admin, normal, observer). Only comments by board members other than kankan's own account are forwarded to an agent; the board's admins are mentioned for cards kankan created. | `GET /1/boards/{id}/memberships?member=true&member_fields=username` |
| B7 | SHOULD | Read who created a card, to mention that person about it. Without it, kankan mentions the board's admins. | `GET /1/cards/{id}/actions?filter=createCard,copyCard,convertToCardFromCheckItem` (`idMemberCreator`, `memberCreator`) |
| B8 | MAY | Resolve a username to a member. Used only by the live test suite. | `GET /1/members/{username}` |

## 4. Lists (columns)

| ID | Level | Capability | Trello endpoint |
|---|---|---|---|
| L1 | MUST | Create a list with a given name on a board. Used by `init` (Backlog, Done, Blocked), by automatic seat creation (three lists per seat) and by `init` and `doctor` for the `kankan` settings list ([board settings §2](boardsettings_spec_functional.md#2-board-settings)). Existing lists with the same name are found through the B3 read and adopted (after confirmation in `init`). After a failed creation the lists are read again to find one that the call itself created; when the user declined to adopt an existing list, the lists that existed before the call are never taken for it. | `POST /1/lists` (`name`, `idBoard`, `pos`) |
| L2 | MUST | Archive or unarchive a list. Used by `seat remove` and by test cleanup. | `PUT /1/lists/{id}/closed` |
| L3 | MUST | Detect that a configured list was archived or deleted. | `GET /1/lists/{id}` (`closed`, or 404) |
| L4 | MUST | Unlimited, or at least several dozen, lists per board, and lists created by humans that kankan ignores. | — |
| L5 | MUST | Ordered cards within a list, readable as a position value. kankan treats the top card of Backlog as highest priority. | card `pos` field |
| L6 | SHOULD | Read the open cards of one list with name, description, labels, last activity and checklists (items, their state and position), and the server's time of the response. The settings list is read every 30 seconds ([board settings §2](boardsettings_spec_functional.md#2-board-settings)). Required, with C7 and C8, for the model and effort settings; without them the agents run with the seeds and `doctor` reports it. | `GET /1/lists/{id}/cards?fields=name,desc,idLabels,dateLastActivity&checklists=all`, `Date` response header |

## 5. Cards

| ID | Level | Capability | Trello endpoint |
|---|---|---|---|
| C1 | MUST | Read one card by ID with list, labels, attachments, title, description, URL, archived state and last activity. Used for the reread after a claim and for verification after every move. | `GET /1/cards/{id}?attachments=true` |
| C2 | MUST | Resolve a card from a short, URL-safe, globally unique code that is stable across board moves. kankan puts this code in branch names. | `shortLink`; `GET /1/cards/{shortLink}` |
| C3 | MUST | Move a card to another list of the same board and place it at the top. kankan verifies every move with a C1 reread. | `PUT /1/cards/{id}` (`idList`, `pos=top`) then `GET /1/cards/{id}` |
| C4 | MUST | Report whether a card is archived or deleted. | `closed` field, or 404 |
| C5 | MUST | A canonical human-readable card URL, used in merge commit messages and comments. | `url` field |
| C6 | SHOULD | Card creation (title, description, top or bottom of a list) and deletion through the API. `kankan merge-request` creates a card at the top of `Backlog`; `init` and `doctor` create the setting cards of the `kankan` list ([board settings §2](boardsettings_spec_functional.md#2-board-settings)). Deletion is used only by the live test suites. Without creation, merge requests and setting cards are created by hand. | `POST /1/cards` (`idList`, `name`, `desc`, `pos`), `DELETE /1/cards/{id}` |
| C7 | SHOULD | Create a named checklist on a card. The model and effort setting cards have one checklist per agent type ([board settings §3](boardsettings_spec_functional.md#3-model-and-effort)). Without checklists, those settings cannot be held on the board. | `POST /1/cards/{id}/checklists` (`name`) |
| C8 | SHOULD | Add a checklist item, ticked or not, move it and delete it; ticking from the UI updates the card's last activity. Used for creating the setting cards and keeping their items in sync with the adapters, in the adapters' order. | `POST /1/checklists/{id}/checkItems` (`name`, `checked`), `PUT /1/cards/{id}/checkItem/{idCheckItem}` (`pos`), `DELETE /1/checklists/{id}/checkItems/{idCheckItem}` |

## 6. Labels

| ID | Level | Capability | Trello endpoint |
|---|---|---|---|
| T1 | MUST | Create a board label with a name and colour. Used for HOLD, Question, Error and the dynamic `Resume YYYY-MM-DD hh:mm` labels; labels with those names already on the board are adopted. | `POST /1/labels` (`idBoard`, `name`, `color`) |
| T2 | MUST | Delete a board label. Used to remove unused or expired `Resume` labels. | `DELETE /1/labels/{id}` |
| T3 | MUST | Add and remove a label on a card by label ID. | `POST /1/cards/{id}/idLabels`, `DELETE /1/cards/{id}/idLabels/{idLabel}` |
| T4 | MUST | Labels keep their ID when the user renames or recolours them. | — |
| T5 | MUST | Users can add labels to cards from the UI. HOLD is the user's command channel. | — |

## 7. Comments and notifications

| ID | Level | Capability | Trello endpoint |
|---|---|---|---|
| M1 | MUST | Add a plain-text or Markdown comment to a card. | `POST /1/cards/{id}/actions/comments` |
| M2 | MUST | List a card's comments with ID, author ID, author username, creation timestamp and text, newest first, and fetch only comments after a given point (ID or timestamp), with pagination. Used to forward the user's answers to a waiting agent and to find kankan's own comment (by its `kankan:<op-id>` marker) after a comment post timed out. | `GET /1/cards/{id}/actions?filter=commentCard&since=<action id>&before=<action id>&limit=50` |
| M3 | MUST | Mentioning a user in a comment (e.g. `@username`) sends that user a notification (app, push or email). This is how kankan alerts the user about Question, Error and Blocked. | Trello `@mention` behaviour |
| M4 | SHOULD | React to a comment or reply to it, to acknowledge that a comment was forwarded. Workaround: post a new comment. | `POST /1/actions/{id}/reactions` |
| M5 | MUST | Identify the authenticated user (ID and username). | `GET /1/members/me` |
| M6 | SHOULD | Delete a comment written by kankan's own account. kankan keeps only its latest 10 comments on each setting card ([board settings §2](boardsettings_spec_functional.md#2-board-settings)). Without it, setting cards accumulate kankan's comments. | `DELETE /1/cards/{id}/actions/{idAction}/comments` |

## 8. Card-to-card links (dependencies)

| ID | Level | Capability | Trello endpoint |
|---|---|---|---|
| D1 | MUST | A user can link a card to another existing card from the UI. kankan treats every linked card as a dependency. | Trello "Add > Attachment > Trello card" |
| D2 | MUST | The link is stored only on the card that declares it. It must not create an automatic reverse link, which kankan would read as a dependency cycle. | — |
| D3 | MUST | The API returns the links with the target card resolvable to its ID or short code, including targets on other boards and archived targets. | attachments with `url` = `https://trello.com/c/{shortLink}/...` |
| D4 | MAY | Native typed relations ("blocks", "is blocked by"). An adapter can map them to D1–D3. | — |

## 9. Card attachments

Files and links the user attaches to a card are task inputs for the agent ([spec §13.3](initial_spec_functional.md#133-card-attachments)). Card-to-card links are dependencies ([section 8](#8-card-to-card-links-dependencies)), not task inputs.

| ID | Level | Capability | Trello endpoint |
|---|---|---|---|
| F1 | SHOULD | A user can attach files and links to a card from the UI. Without it, the user puts text in the description or links it there. | Trello "Add > Attachment" (computer upload or link); live test suite only: `POST /1/cards/{id}/attachments` (multipart `file`, or `url` + `name`) |
| F2 | SHOULD | List a card's attachments with ID, name, file name (stable when the attachment is renamed), URL, size, MIME type, the member who added it, and whether it is an uploaded file or a link. kankan passes to the agent only attachments added by board members other than its own account. | `GET /1/cards/{id}/attachments?fields=id,name,fileName,url,bytes,mimeType,idMember,isUpload,pos`, plus [B6](#3-boards) for membership |
| F3 | SHOULD | Download the content of an uploaded file with the API credential, from a URL on the API host, without sending the credential to another host. Without it, the agent sees only the attachments' names and URLs. | `GET /1/cards/{id}/attachments/{idAttachment}/download/{fileName}` on the API host with the `Authorization` header. The path is taken from the attachment's `url` after validation; a redirect to another origin is followed without the credential, https only, and no later hop carries the credential, even back on the API host. |

## 10. Authentication bootstrap

| ID | Level | Capability | Trello |
|---|---|---|---|
| A1 | MUST | A documented manual procedure through which a user obtains a non-expiring (or long-lived, renewable) token, which kankan stores in a properties file. | API key from `trello.com/apps/admin`; token from `trello.com/1/authorize?expiration=never&scope=read,write&response_type=token&key=...` |
| A2 | MUST | A cheap endpoint that validates the credential. | `GET /1/members/me` |

## 11. Requirements outside the API

- **UI:** users create and reorder cards, create extra lists, add and remove labels, link cards, attach files and links, and write comments from a web or mobile UI. kankan provides no UI of its own.
- **Visibility:** the UI shows labels, comments and list membership on the card front or in the card detail.
- **Data model:** each card is in exactly one list at a time.
- **Free tier:** the use case (one board per project, a few dozen lists, low thousands of cards) should fit a free or low-cost plan, as it does on Trello.

## 12. Adapter checklist

An adapter for a new backend is complete when it implements every MUST above behind the interface of kankan's `trello` module, and the `trello_live` tests ([spec §18.2](initial_spec_technical.md#182-test-board)) pass against a dedicated test board of that product.

## Board-settings transport implementation

The settings transport reads each list's `closed` field with L3 before L6, whose fields also include `idBoard,idList,pos,closed,url,shortLink`. B3 returns all checklists at board level with `idCard`; C1 requests nested `checklists=all` for construction recovery. Checklist creation recovers by C1/name, item creation by `GET /1/checklists/{id}?checkItems=all`/name. C8 creation also supplies `pos`. L2 supports restoration with `value=false`. M6 deletion treats 404 as success. All read dates belong to their individual response. Init and online fixing doctor use L1/C6–C8 to build/repair settings; settings feedback uses T3/M1/M2/M6.
