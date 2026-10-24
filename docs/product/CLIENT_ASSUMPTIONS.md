# Client assumptions

| Assumption | Status |
|---|---|
| Client is Russian-speaking, manages architecture/interior projects, suppliers, factories, estimates | ASSUMED (from spec) |
| Primary mail is Gmail / Google Workspace | NEEDS_CLIENT_INPUT |
| Project «Флора» exists; people, prices, sizes in fixtures | SYNTHETIC — removed from the app on 2026-10-24 (the user does not need it) |
| Estimates arrive as XLSX from suppliers | ASSUMED |
| Drawings arrive as PDF/DWG; demo uses SVG plans | NEEDS_CLIENT_INPUT (formats actually used) |
| Client wants approval before any outgoing message | CONFIRMED by spec (hard rule) |
| Cloud AI (Google Gemini) is acceptable for non-sensitive requests | NEEDS_CLIENT_INPUT (privacy mode is selectable) |
| Signature/name for outgoing letters | NEEDS_CLIENT_INPUT (drafts end with «С уважением,») |
| Working language of contractors: Russian; some suppliers in EUR | ASSUMED |
| Client (Alina) orders fashion from factories for customers AA / IDS / SS…; factory files are Excel (a sheet per brand, photos, «AA 40, Alina 38» allocations) and PDF proformas | CONFIRMED by the user's brief (2026-10-25) and the alina-fashion-ai project |
| Customer markups differ from the factory's; deposits are 50 % of customer and factory sums; the owner pays the difference | CONFIRMED (brief) |
| The client says customer codes as letters («ай-ди-эс»); «АА» may need a spoken alias | ASSUMED — GigaAM loses «эй эй» / «А-А» (synthetic voice); NEEDS_CLIENT_INPUT |
| Factory PDFs alone (without Excel) must create orders | NEEDS_CLIENT_INPUT — today a PDF is matched by article with orders from Excel |
