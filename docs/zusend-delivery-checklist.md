# Zusend delivery checklist

This is the implementation checklist for the internal Heaventree workspace. Develop and verify against Zulip 12.4. Dokploy deployment is deferred.

| Workstream | Status | Acceptance still to verify |
|---|---|---|
| Persistent composer | Implemented | Windows desktop and browser walkthrough for expand, send, reload, minimise and narrow-window focus. |
| DM and channel-topic switching | Composer follows the active conversation when expanded. Sidebar shortcuts jump straight to the relevant list. | Repeated DM → topic → DM with a short draft, completed attachment, unread state and scroll restoration. Check groups, private channels and guests. |
| Screenshot attachments | Inline preview for completed image uploads, including restored drafts. Existing upload progress and failure handling apply. | Windows Snipping Tool paste, image removal affordance, retry and draft switching while uploading. |
| Workspace design | Inbox, Mentions, Saved and Drafts are directly available in the workspace sidebar. | Channel/topic header, visual hierarchy, responsive context panel and activity views. |
| Android | Separate Flutter workstream. | Source and release baseline, then device tests for navigation, drafts, uploads, back navigation and push links. |
| Support | Signed OpenWA webhook endpoint plus transactional development storage for queue views, case claims and resolution, internal notes and queued replies. | Synthetic staff preview supports claim, note, queued reply and resolution. Integrate Zulip staff authentication, the sending worker and media retrieval, then verify test-number traffic. |
| Apps and workflows | Planned around existing Zulip bots, webhook and event integration points. | Approved installation, secret storage, destination permissions, health, test run, retry and audit. |
| Shared files and meetings | Files and Links shortcuts use the existing access-checked search. Meetings remain on the call-provider workstream. | Verify scoped search in each conversation type. Add a richer shared-content view, meeting metadata and provider handoffs. |

## Pinned blockers and decisions

- **Live verification is deferred.** No Dokploy deployment is required during implementation. Browser/device walkthroughs can use an isolated fixture environment or a later approved staging deployment.
- **Local full-stack runtime on this host.** The Docker CLI cannot reach a Linux engine, and Docker Desktop startup reports a missing installation registry key. Focused frontend and support tests run locally. A full in-browser walkthrough needs a working Linux development runtime or an authorised test host.
- **Android build validation.** The mobile client has a separate repository and build pipeline. Record the installed Android version and confirm the pilot APK signing and update path before distribution.
- **WhatsApp provider validation.** A dedicated test number and a non-production route are needed for a real send/receipt test. Use synthetic webhook fixtures in the meantime. Keep the primary customer number outside the pilot until the production transport is chosen.
- **Windows draft suite locale.** The date formatting assertion expects US month-first strings and uppercase AM/PM. The Windows environment produced day-first strings and lowercase am/pm. Check the suite in the normal Linux CI environment before making any upstream test fixture changes.

Keep this checklist current as changes land. Work on independent implementation and synthetic tests while the validation items are pending.

## Support development checks

Run `python -m unittest discover -s zusend_support -v` from the repository root. The fixtures exercise signed OpenWA events, retries, delivery receipts arriving out of order, transactional failure, single-agent assignment and reply intent deduplication. The prototype uses SQLite for local verification. The WSGI webhook factory accepts signed events for one configured session at /webhooks/openwa. The staff API, role integration, outbound worker, media retention and production storage configuration are still pending.

The adapter is based on the [WhatsAppOS webhook contract](https://github.com/heaventree/WhatsAppOS/blob/82189083f4c260fe4e432121d2b79991851549a1/docs/06-api-specification.md#66-webhook-events--delivery-semantics). The fixtures use a synthetic session, payloads and signing key.

### Run the staff Support preview

Run `python -m zusend_support.preview` from the repository root and open `http://127.0.0.1:8765`. The preview binds to loopback and seeds a temporary database with three synthetic cases. Staff actions exercise the same local inbox storage used by the signed webhook endpoint. The preview uses a synthetic agent and never sends WhatsApp traffic. Restart the preview to reset the fixtures.
