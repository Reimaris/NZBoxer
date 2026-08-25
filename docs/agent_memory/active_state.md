# Active State — NZBoxer

**Last Updated:** 2026-08-25  
**Current Phase:** Bounded Upgrade Search Cycles & Target Cutoff Alignment

---

## 🎯 Active Milestone: Bounded Upgrade Search Cycles & Target Cutoff Alignment
**Specification:** [`./docs/agent_memory/current_spec.md`](file:///home/richard/Dokumente/Coding/NZBoxer_dev/docs/agent_memory/current_spec.md) (Status: Ready for Implementation)

Tracer-bullet tickets published to `.scratch/bounded-upgrade-cycles/issues/`:

- [x] **[01-data-model-migration-settings.md](file:///home/richard/Dokumente/Coding/NZBoxer_dev/.scratch/bounded-upgrade-cycles/issues/01-data-model-migration-settings.md)**: Data Model Migration & Configurable Max Upgrade Attempts Setting (Completed ✅)
- [x] **[02-video-automation-upgrade-exhaustion.md](file:///home/richard/Dokumente/Coding/NZBoxer_dev/.scratch/bounded-upgrade-cycles/issues/02-video-automation-upgrade-exhaustion.md)**: Target Cutoff Standardization & Video Automation Upgrade Attempt Exhaustion (Completed ✅)
- [x] **[03-print-automation-upgrade-exhaustion.md](file:///home/richard/Dokumente/Coding/NZBoxer_dev/.scratch/bounded-upgrade-cycles/issues/03-print-automation-upgrade-exhaustion.md)**: Print Media Upgrade Attempt Exhaustion (Completed ✅)
- [ ] **[04-dashboard-progress-and-sublabels.md](file:///home/richard/Dokumente/Coding/NZBoxer_dev/.scratch/bounded-upgrade-cycles/issues/04-dashboard-progress-and-sublabels.md)**: Dashboard Attempt Progress Badges & Cutoff-Unmet Sub-Labels (Ready 🟢)

### 📋 Completed Milestone: Multi-Category Provider Architecture
 
 Tracer-bullet tickets published to `.scratch/provider-settings-redesign/issues/`:
 
- [x] **[01-unified-provider-model-migration.md](file:///home/richard/Dokumente/Coding/NZBoxer_dev/.scratch/provider-settings-redesign/issues/01-unified-provider-model-migration.md)**: Unified Provider Model & Legacy Keys Auto-Migration (Completed ✅)
- [x] **[02-dynamic-service-layer-integration.md](file:///home/richard/Dokumente/Coding/NZBoxer_dev/.scratch/provider-settings-redesign/issues/02-dynamic-service-layer-integration.md)**: Dynamic Provider Service Layer Integration (Completed ✅)
- [x] **[03-multi-indexer-cascade-downloader-rule.md](file:///home/richard/Dokumente/Coding/NZBoxer_dev/.scratch/provider-settings-redesign/issues/03-multi-indexer-cascade-downloader-rule.md)**: Multi-Indexer Sequential Cascade & Single-Active Downloader Rule (Completed ✅)
- [x] **[04-visual-provider-catalog-modal-flow.md](file:///home/richard/Dokumente/Coding/NZBoxer_dev/.scratch/provider-settings-redesign/issues/04-visual-provider-catalog-modal-flow.md)**: Visual Provider Catalog & Dynamic Modal Flow (Completed ✅)
- [x] **[05-categorized-settings-ui-cleanup.md](file:///home/richard/Dokumente/Coding/NZBoxer_dev/.scratch/provider-settings-redesign/issues/05-categorized-settings-ui-cleanup.md)**: Categorized Settings UI & Global Settings Cleanup (Completed ✅)
- [x] **Card Visual Polish & Simkl Quality Options Restoration**: Polished responsive card headers, footers, badges (`Priority 1`), and full profile options (Completed ✅)
- [x] **Default Min/Max Size Values Recovery**: Recovered Movies (`500` / `25000`), TV Shows (`200` / `8000`), and Anime (`200` / `8000`) min/max size defaults (Completed ✅)
- [x] **Provider Modal Save & Edit Persistence Fix**: Fixed nested form issue by closing global settings form, bound isolated fieldsets (`:disabled`) per provider type to prevent overlapping input collisions, and corrected `ProviderProfile.media_type` string resolution in templates (Completed ✅)
- [x] **Dashboard System Check Provider Resolution Fix**: Resolved TMDB, TreasureMaps, and TorBox credentials directly from active `Provider` records with fallback to `SystemSettings`, corrected default endpoint URL for TreasureMaps to `https://treasuremaps.net/api`, and improved self-healing download check credential resolution (Completed ✅)
- [x] **Settings Sub-Section Tab Persistence**: Added URL hash/history synchronization (`#providers`, `#notifications`, `#scoring`, `#print`, `#general`) combined with `localStorage` fallback so that creating, editing, toggling, or deleting providers and notifications keeps you in the exact sub-section across reloads (Completed ✅)

## 🚀 Released Versions
- **v2.1.2**: Provider credentials resolution fix for dashboard system check, domain normalization for TreasureMaps, and self-healing asynchronous TorBox key resolution.
- **v2.1.1**: Provider modal save/edit fixes, multi-section fieldset collision isolation, and settings sub-tab URL hash/storage persistence.
- **v2.1.0**: Multi-Category Provider Architecture, 2-Step Provider Catalog, Multi-Indexer Cascading Search, and Settings Redesign.
- **v2.0.0**: Initial major release with full print media automation, fake detector, self-healing, and dashboard.

---

## 🏷️ Current Git Branch
- `main` (synced with `origin/main` at `da7dc88`)


---

## 🔮 Next Steps for Future Sessions
1. **Additional Indexer Types**: Implement native Newznab category customization per indexer provider.
2. **Extended Downloader Adapters**: Prepare base interfaces for optional future downloaders (e.g. SABnzbd / Real-Debrid) if requested.
3. **Observability**: Add structured health metrics and response latency tracking to provider cards.
