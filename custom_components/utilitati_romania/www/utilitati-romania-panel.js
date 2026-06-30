const UTILITATI_ROMANIA_FRONTEND_VERSION = "1.10.7b13";

class UtilitatiRomaniaPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._panel = null;
    this._activeTab = "overview";
    this._expandedLocations = new Set();
    this._expandedInvoices = new Set();
    this._readingCache = new Map();
    this._actions = new Map();
    this._readingDrafts = new Map();
    this._invoiceGrouping = this._loadInvoiceGroupingPreference() || "location";
    this._invoiceFilter = this._loadInvoiceFilterPreference() || "all";
    this._settingsDrafts = new Map();
    this._dashboardDrafts = new Map();
    this._licenseDraft = "";
    this._interactiveUntil = 0;
  }

  set hass(hass) {
    this._hass = hass;
    this._readingCache.clear();
    if (this._shouldDelayRenderForInteraction()) return;
    this._render();
  }

  set panel(panel) {
    this._panel = panel;
    this._render();
  }

  connectedCallback() {
    this._render();
  }

  _shouldDelayRenderForInteraction() {
    if (!this.shadowRoot) return false;
    if (Date.now() < this._interactiveUntil) return true;
    const active = this.shadowRoot.activeElement;
    if (!active) return false;
    return !!active.closest?.("[data-invoice-grouping], [data-invoice-filter], .reading-input, #license-input, [data-mobile-device-select], [data-setting-toggle], [data-location-alias], [data-billing-group], [data-dashboard-pref-text], [data-consumption-visibility]");
  }

  _holdRenderBriefly(ms = 3500) {
    this._interactiveUntil = Date.now() + ms;
  }

  _callServiceWithTimeout(domain, service, data, timeoutMs = 12000) {
    const call = this._hass.callService(domain, service, data);
    const timeout = new Promise((_, reject) => {
      window.setTimeout(() => reject(new Error("timeout")), timeoutMs);
    });
    return Promise.race([call, timeout]);
  }

  _summaryEntityId() {
    const states = this._hass?.states || {};
    const configured = this._panel?.config?.summary_entity;
    const preferred = "sensor.administrare_integrare_facturi_utilitati";

    const isSummaryCandidate = (entityId) => {
      const attrs = states[entityId]?.attributes || {};
      return entityId.startsWith("sensor.")
        && Array.isArray(attrs.locatii)
        && Object.prototype.hasOwnProperty.call(attrs, "numar_facturi")
        && Object.prototype.hasOwnProperty.call(attrs, "total_neplatit");
    };

    const candidates = Object.keys(states).filter(isSummaryCandidate);
    if (!candidates.length) {
      if (configured && states[configured]) return configured;
      if (states[preferred]) return preferred;
      return null;
    }

    const scoreCandidate = (entityId) => {
      const state = states[entityId] || {};
      const attrs = state.attributes || {};
      const locations = Array.isArray(attrs.locatii) ? attrs.locatii : [];
      const providers = locations.reduce((sum, location) => {
        const list = Array.isArray(location?.furnizori) ? location.furnizori : [];
        return sum + list.length;
      }, 0);
      const invoices = Number(attrs.numar_facturi ?? providers ?? 0) || 0;
      const updated = Date.parse(state.last_updated || state.last_changed || "") || 0;

      // În unele instalări Home Assistant poate păstra un entity_id vechi/restored
      // pentru senzorul agregat, iar senzorul activ primește sufix (_2, _3 etc.).
      // Alegem senzorul cu cele mai multe facturi/furnizori și apoi cel mai recent,
      // nu entity_id-ul hardcodat, ca să evităm afișarea datelor vechi în dashboard.
      return invoices * 1000000 + providers * 10000 + Math.floor(updated / 1000);
    };

    candidates.sort((a, b) => {
      const diff = scoreCandidate(b) - scoreCandidate(a);
      if (diff !== 0) return diff;
      if (a === configured) return -1;
      if (b === configured) return 1;
      if (a === preferred) return -1;
      if (b === preferred) return 1;
      return a.localeCompare(b);
    });

    return candidates[0] || null;
  }

  _summary() {
    const entityId = this._summaryEntityId();
    const state = entityId ? this._hass?.states?.[entityId] : null;
    return {
      entityId,
      state,
      attrs: state?.attributes || {},
      locations: Array.isArray(state?.attributes?.locatii) ? state.attributes.locatii : [],
      consumptionPoints: Array.isArray(state?.attributes?.locuri_consum) ? state.attributes.locuri_consum : [],
    };
  }

  _escape(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/\"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  _maskEmail(value) {
    const text = String(value ?? "").trim();
    if (!text || text === "—" || !text.includes("@")) return text || "—";
    const [user, domain] = text.split("@");
    if (!domain) return text;
    const visibleUser = user.length <= 2 ? user.slice(0, 1) : user.slice(0, 2);
    const domainParts = domain.split(".");
    const domainName = domainParts[0] || "";
    const suffix = domainParts.slice(1).join(".");
    const visibleDomain = domainName.length <= 2 ? domainName.slice(0, 1) : domainName.slice(0, 2);
    return `${visibleUser}***@${visibleDomain}***${suffix ? `.${suffix}` : ""}`;
  }

  _maskLicense(value) {
    const text = String(value ?? "").trim();
    if (!text || text === "—") return text || "—";
    if (text.length <= 8) return "****";
    return `${text.slice(0, 4)}-****-****-${text.slice(-4)}`;
  }

  _safeDiagnosticLicense(license) {
    return {
      status: license?.status || "necunoscut",
      plan: license?.plan || "—",
      account: this._maskEmail(license?.account || "—"),
      checked: license?.checked || "—",
      key: license?.key || "—",
      message: license?.message || "—",
    };
  }

  _normalizeText(value) {
    return String(value ?? "")
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "")
      .toLowerCase()
      .replace(/[_-]+/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  _num(value) {
    if (typeof value === "number") return Number.isFinite(value) ? value : 0;
    if (typeof value === "string") {
      const parsed = Number(value.replace(/\s/g, "").replace(",", "."));
      return Number.isFinite(parsed) ? parsed : 0;
    }
    return 0;
  }

  _money(value, currency = "RON") {
    if (value === null || value === undefined || value === "") return "—";
    try {
      return new Intl.NumberFormat("ro-RO", {
        style: "currency",
        currency,
        maximumFractionDigits: 2,
      }).format(this._num(value));
    } catch (_err) {
      return `${this._num(value).toFixed(2)} ${currency}`;
    }
  }

  _pluralRo(count, singular, plural) {
    return `${count} ${count === 1 ? singular : plural}`;
  }

  _date(value) {
    if (!value || value === "-") return "—";
    const text = String(value).trim();
    if (/^\d{2}\.\d{2}\.\d{4}$/.test(text)) return text;
    const parsed = new Date(text);
    if (!Number.isNaN(parsed.getTime())) {
      try {
        return new Intl.DateTimeFormat("ro-RO").format(parsed);
      } catch (_err) {
        return text;
      }
    }
    return text || "—";
  }

  _parseDateLike(value) {
    if (!value) return null;
    if (value instanceof Date && !Number.isNaN(value.getTime())) return value;
    const text = String(value).trim();
    let parsed = null;
    if (/^\d{2}\.\d{2}\.\d{4}$/.test(text)) {
      const [dd, mm, yyyy] = text.split(".");
      parsed = new Date(`${yyyy}-${mm}-${dd}T00:00:00`);
    } else if (/^\d{4}-\d{2}-\d{2}/.test(text)) {
      parsed = new Date(text);
    } else if (/^\d{2}\/\d{2}\/\d{4}$/.test(text)) {
      const [dd, mm, yyyy] = text.split("/");
      parsed = new Date(`${yyyy}-${mm}-${dd}T00:00:00`);
    } else {
      parsed = new Date(text);
    }
    return parsed && !Number.isNaN(parsed.getTime()) ? parsed : null;
  }

  _todayDate() {
    const today = new Date();
    return new Date(today.getFullYear(), today.getMonth(), today.getDate());
  }

  _daysUntil(value) {
    const parsed = this._parseDateLike(value);
    if (!parsed) return null;
    const due = new Date(parsed.getFullYear(), parsed.getMonth(), parsed.getDate());
    return Math.round((due.getTime() - this._todayDate().getTime()) / 86400000);
  }

  _makeKey(...parts) {
    return parts.map((part) => String(part ?? "").trim()).join("__");
  }

  _storageKey(name) {
    return `utilitati_romania_panel__${name}`;
  }

  _loadJsonPreference(name, fallback) {
    try {
      const raw = window.localStorage?.getItem(this._storageKey(name));
      if (!raw) return fallback;
      return JSON.parse(raw);
    } catch (_err) {
      return fallback;
    }
  }

  _saveJsonPreference(name, value) {
    try { window.localStorage?.setItem(this._storageKey(name), JSON.stringify(value)); } catch (_err) {}
  }

  _dashboardPreferences() {
    return {
      defaultTab: "overview",
      compactInvoicesMobile: true,
      backButtonEnabled: false,
      backButtonLabel: "Înapoi",
      backButtonTarget: "/lovelace",
      ...this._loadJsonPreference("dashboard_preferences", {}),
    };
  }

  _backButtonPreferences() {
    const prefs = this._dashboardPreferences();
    const label = String(prefs.backButtonLabel || "Înapoi").trim() || "Înapoi";
    const target = String(prefs.backButtonTarget || "/lovelace").trim() || "/lovelace";
    return {
      enabled: Boolean(prefs.backButtonEnabled),
      label,
      target,
    };
  }

  _navigateDashboardBack() {
    const prefs = this._backButtonPreferences();
    const safeTarget = String(prefs.target || "/lovelace").trim();
    if (["back", "history", "history.back"].includes(safeTarget.toLowerCase())) {
      window.history.back();
      return;
    }
    if (/^https?:\/\//i.test(safeTarget)) {
      window.open(safeTarget, "_self", "noopener,noreferrer");
      return;
    }
    const target = safeTarget.startsWith("/") ? safeTarget : `/${safeTarget}`;
    window.history.pushState(null, "", target);
    window.dispatchEvent(new CustomEvent("location-changed"));
  }

  _notificationPreferences() {
    return {
      facturi_noi: true,
      scadente: true,
      indexuri: true,
      praguri_scadenta: [5, 3, 1],
      ...this._loadJsonPreference("notification_preferences", {}),
    };
  }

  _locationAliases() {
    return this._loadJsonPreference("location_aliases", {});
  }

  _locationKey(location) {
    return String(location?.locatie_cheie || location?.eticheta_locatie || location?.nume || location?.id || "locatie").trim();
  }

  _rawLocationName(location) {
    return location?.eticheta_locatie || location?.nume || location?.locatie_cheie || "Locație";
  }

  _toDisplayCase(value) {
    return String(value || "")
      .toLowerCase()
      .replace(/\b([a-zăâîșşțţ])/gi, (match) => match.toUpperCase());
  }

  _cleanTechnicalLocationCandidate(value) {
    let text = String(value ?? "").replace(/\s+/g, " ").trim();
    if (!text) return "";

    const normalized = this._normalizeText(text);
    const isTechnicalApaBrasov =
      normalized.includes("corespondenta") &&
      normalized.includes("apa rece contorizata") &&
      normalized.includes("strada");

    if (!isTechnicalApaBrasov) return text;

    const streetMatch = text.match(/\bstrada\s+(.+?)(?:\s*,?\s*(?:loc|jud)\b|\s*\((?:exc|excel).*?$|$)/i);
    if (!streetMatch) return text;

    let segment = streetMatch[1].replace(/\s+/g, " ").trim().replace(/[;,.:\-]+$/g, "");
    segment = segment.replace(/\bBRASOV\b.*$/i, "").trim().replace(/[;,.:\-]+$/g, "");

    let number = "";
    const numberMatch = segment.match(/(?:^|[\s,])nr\.?\s*[:\-]?\s*([0-9]+)\s*([a-z])?\b/i);
    if (numberMatch) {
      number = `${numberMatch[1]}${numberMatch[2] || ""}`.toUpperCase();
      segment = segment.slice(0, numberMatch.index).trim().replace(/[;,.:\-]+$/g, "");
    } else {
      const inlineNumber = segment.match(/^(.*?)[\s,]+([0-9]+)\s*([a-z])?$/i);
      if (inlineNumber) {
        segment = inlineNumber[1].trim();
        number = `${inlineNumber[2]}${inlineNumber[3] || ""}`.toUpperCase();
      }
    }

    segment = segment
      .replace(/\b(?:nu|nedeterminat|apa rece contorizata|brasov populatie|populatie)\b/gi, " ")
      .replace(/[^a-zA-ZăâîșşțţĂÂÎȘŞȚŢ0-9 .'-]+/g, " ")
      .replace(/\s+/g, " ")
      .trim();

    if (!segment) return text;
    const street = this._toDisplayCase(segment);
    return number ? `${street} ${number}` : street;
  }

  _cleanLocationCandidate(value) {
    const text = String(value ?? "").trim();
    if (!text || ["-", "—", "none", "null", "undefined"].includes(text.toLowerCase())) return "";
    return this._cleanTechnicalLocationCandidate(text).replace(/\s+/g, " ").trim();
  }

  _locationLabelScore(value) {
    const text = this._cleanLocationCandidate(value);
    if (!text) return -1;
    const normalized = this._normalizeText(text);
    let score = Math.min(text.length, 120) / 10;
    if (/\d/.test(text)) score += 12;
    if (/\b(?:nr|numar|numarul)\.?\s*[:\-]?\s*\d/i.test(text)) score += 8;
    if (/\b(?:bl|bloc)\.?\s*[:\-]?\s*[a-z0-9]/i.test(text)) score += 4;
    if (/\b(?:sc|scara)\.?\s*[:\-]?\s*[a-z0-9]/i.test(text)) score += 4;
    if (/\b(?:et|etaj)\.?\s*[:\-]?\s*[a-z0-9]/i.test(text)) score += 3;
    if (/\b(?:ap|apt|apartament)\.?\s*[:\-]?\s*[a-z0-9]/i.test(text)) score += 14;
    if (/\b(strada|str\.?|bd\.?|bulevard|calea|aleea|sos\.?|soseaua|intrarea|drumul)\b/i.test(text)) score += 4;
    if (/\b(?:telecomunicatii|energie electrica|gaze naturale|salubritate|ultima factura|regularizare|estimare)\b/i.test(normalized)) score -= 10;
    if (/^\d+[a-z]?\s*,\s*[a-z]/i.test(text)) score += 8;
    return score;
  }

  _bestLocationLabel(candidates) {
    const seen = new Set();
    let best = "";
    let bestScore = -1;

    for (const value of candidates || []) {
      const cleaned = this._cleanLocationCandidate(value);
      if (!cleaned) continue;
      const key = this._normalizeText(cleaned);
      if (seen.has(key)) continue;
      seen.add(key);
      const score = this._locationLabelScore(cleaned);
      if (score > bestScore) {
        best = cleaned;
        bestScore = score;
      }
    }

    return best;
  }

  _locationCandidates(location, provider = null) {
    const candidates = [
      location?.eticheta_locatie,
      location?.locatie_label,
      location?.nume,
      location?.adresa_originala,
      location?.address,
      location?.locatie_cheie,
    ];

    const providers = provider ? [provider] : Array.isArray(location?.furnizori) ? location.furnizori : [];
    for (const item of providers) {
      candidates.push(
        item?.adresa_originala,
        item?.adresa,
        item?.address,
        item?.service_address,
        item?.nume_cont,
        item?.locatie_label,
        item?.eticheta_locatie,
      );
    }

    return candidates;
  }

  _displayLocationName(location) {
    const override = this._cleanLocationCandidate(location?._dashboard_display_name);
    if (override) return override;
    const key = this._locationKey(location);
    const aliases = this._locationAliases();
    const alias = this._cleanLocationCandidate(aliases[key]);
    if (alias) return alias;
    return this._bestLocationLabel(this._locationCandidates(location)) || String(this._rawLocationName(location) || "Locație").trim();
  }

  _displayLocationGroupKey(location) {
    const label = this._displayLocationName(location);
    const normalized = this._normalizeText(label);
    return normalized || this._locationKey(location) || "locatie";
  }

  _billingDisplayName(location, provider = null) {
    const manualFromProvider = this._cleanLocationCandidate(provider?.eticheta_grupare_manuala || provider?.grupare_facturi || provider?.billing_group);
    if (manualFromProvider) return manualFromProvider;

    const matched = provider ? this._matchedBillingGroupEntity(location, provider) : null;
    const saved = this._cleanLocationCandidate(matched?.savedValue);
    if (saved) return saved;

    return this._displayLocationName(location);
  }

  _displayLocations(locations) {
    const groups = new Map();

    for (const location of locations || []) {
      const providers = Array.isArray(location?.furnizori) ? location.furnizori : [];

      if (!providers.length) {
        const label = this._displayLocationName(location);
        const key = this._displayLocationGroupKey(location);
        if (!groups.has(key)) {
          groups.set(key, {
            ...location,
            _dashboard_display_name: label,
            _dashboard_source_locations: [location],
            furnizori: [],
            total_neplatit: this._num(location?.total_neplatit),
          });
        }
        continue;
      }

      for (const provider of providers) {
        const label = this._billingDisplayName(location, provider);
        const normalized = this._normalizeText(label);
        const key = normalized || this._displayLocationGroupKey(location);
        if (!groups.has(key)) {
          groups.set(key, {
            ...location,
            locatie_cheie: key,
            eticheta_locatie: label,
            _dashboard_display_name: label,
            _dashboard_source_locations: [],
            furnizori: [],
            total_neplatit: 0,
          });
        }

        const group = groups.get(key);
        group._dashboard_source_locations.push(location);
        group.furnizori.push(provider);
        if (this._status(provider) === "unpaid") {
          group.total_neplatit += this._providerUnpaidTotal(provider);
        }
      }
    }

    return Array.from(groups.values()).map((location) => ({
      ...location,
      total_neplatit_formatat: this._money(location.total_neplatit, "RON"),
    }));
  }

  _mobileDeviceSelectEntity() {
    return this._mobileSelectEntityByPurpose("deschidere furnizori");
  }

  _mobileNotificationSelectEntity() {
    return this._mobileSelectEntityByPurpose("notificari");
  }

  _mobileSelectEntityByPurpose(purpose) {
    const states = Object.values(this._hass?.states || {});
    const wanted = this._normalizeText(purpose || "");
    let best = null;
    let bestScore = -1;
    for (const stateObj of states) {
      if (!stateObj?.entity_id?.startsWith("select.")) continue;
      const text = this._entityFriendlyText(stateObj);
      const entityId = String(stateObj.entity_id || "").toLowerCase();
      let score = 0;
      if (entityId.includes("utilitati_romania") || entityId.includes("administrare_integrare")) score += 40;
      if (text.includes("dispozitiv mobil")) score += 60;
      if (wanted && text.includes(wanted)) score += 100;
      if (Array.isArray(stateObj.attributes?.options) && stateObj.attributes.options.some((item) => String(item).startsWith("mobile_app_"))) score += 40;
      if (score > bestScore) {
        best = stateObj;
        bestScore = score;
      }
    }
    return bestScore >= 140 ? best : null;
  }

  _mobileDeviceLabel(option) {
    const value = String(option || "");
    if (!value || value === "none") return "Neselectat";
    return value.replace(/^mobile_app_/, "").replace(/_/g, " ");
  }

  _entityFriendlyText(stateObj) {
    const friendly = stateObj?.attributes?.friendly_name || "";
    return this._normalizeText(`${stateObj?.entity_id || ""} ${friendly}`);
  }

  _textMatchesAny(text, terms) {
    const hay = this._normalizeText(text || "");
    return (terms || []).some((term) => term && hay.includes(term));
  }

  _providerName(provider) {
    return provider?.furnizor_label || provider?.furnizor || provider?.provider || "Furnizor";
  }

  _hidroelectricaLocationLabel(location, provider) {
    const candidates = [
      provider?.nume_cont,
      location?.locatie_label,
      location?.eticheta_locatie,
      location?.name,
      provider?.adresa_originala,
    ]
      .map((value) => String(value ?? "").trim())
      .filter((value) => value && !["-", "—", "none", "null", "undefined"].includes(value.toLowerCase()));

    const parseStreetNumber = (raw) => {
      const source = String(raw || "");
      const apartmentMatch = source.match(/\b(?:ap|apt|apartament)\.?\s*[:\-]?\s*([A-Za-z0-9\-/]+)/i);
      const apartmentSuffix = apartmentMatch ? ` ap. ${String(apartmentMatch[1] || "").trim()}` : "";
      const parts = source.split(/[;,]+/).map((part) => part.trim()).filter(Boolean);
      if (parts.length >= 2 && /^\d+[a-z]?$/i.test(parts[0])) {
        return `${parts[1]} ${parts[0]}${apartmentSuffix}`.replace(/\s+/g, " ").trim();
      }
      const streetMatch = source.match(/(?:str(?:ada)?\.?\s*)?([A-ZĂÂÎȘŞȚŢa-zăâîșşțţ0-9 .'-]+?)\s*(?:nr\.?\s*)?(\d+\s*[A-Za-z]?)(?:\b|,)/i);
      if (streetMatch) {
        const strada = String(streetMatch[1] || "").replace(/^(strada|str\.)\s+/i, "").trim();
        const numar = String(streetMatch[2] || "").replace(/\s+/g, "").trim();
        if (strada && numar) return `${strada} ${numar}${apartmentSuffix}`.trim();
      }
      return "";
    };

    for (const candidate of candidates) {
      const parsed = parseStreetNumber(candidate);
      if (parsed) return parsed;
    }

    const raw = candidates[0] || "";
    if (!raw) return "";
    const parts = raw.split(/[;,]+/).map((part) => part.trim()).filter(Boolean);
    return parts.slice(0, 2).join(" ").replace(/\s+/g, " ").trim() || raw;
  }

  _providerDisplayName(location, provider) {
    const base = this._providerName(provider);
    if (this._providerKey(provider) === "hidroelectrica") {
      const locatie = this._hidroelectricaLocationLabel(location, provider);
      if (locatie && this._normalizeText(locatie) !== this._normalizeText(base)) return `${base} - ${locatie}`;
    }
    return base;
  }

  _invoiceLocationLine(location, provider, displayName, grouping) {
    if (grouping === "location") return "";

    const locationName = this._bestLocationLabel(this._locationCandidates(location, provider)) || this._displayLocationName(location);
    if (!locationName) return "";

    const normalizedLocation = this._normalizeText(locationName);
    const normalizedDisplay = this._normalizeText(displayName);
    const normalizedInvoice = this._normalizeText(this._providerInvoice(provider));

    if (normalizedLocation && normalizedDisplay.includes(normalizedLocation)) return "";
    if (normalizedLocation && normalizedInvoice.includes(normalizedLocation)) return "";

    return `<span class="invoice-location">${this._escape(locationName)}</span>`;
  }

  _providerUtilityType(provider) {
    const candidates = [
      provider?.tip_utilitate,
      provider?.tip_serviciu,
      provider?.service_type,
      provider?.serviciu,
      provider?.description,
      provider?.invoice_description,
      provider?.categorie,
      provider?.utility_type,
    ];
    const raw = candidates.map((value) => String(value ?? "").trim()).find((value) => value && !["-", "—", "none", "null", "undefined"].includes(value.toLowerCase()));
    if (!raw) return "";

    const normalized = this._normalizeText(raw);
    if (normalized.includes("digi energy") || normalized.includes("energie") || normalized.includes("electric") || normalized === "curent") return "Energie electrică";
    if (normalized.includes("telecom") || normalized.includes("internet") || normalized.includes("telefon") || normalized.includes("tv")) return "Telecomunicații";
    if (normalized.includes("apa") || normalized.includes("canal")) return "Apă / canal";
    if (normalized.includes("gaz")) return "Gaze naturale";
    if (normalized.includes("salubritate") || normalized.includes("deseuri") || normalized.includes("gunoi")) return "Salubritate";

    return raw.replace(/[_-]+/g, " ").replace(/\s+/g, " ").trim();
  }

  _providerKey(provider) {
    return String(provider?.furnizor || provider?.provider || this._providerName(provider)).trim().toLowerCase();
  }


  _providerIdentityTerms(provider) {
    return [
      provider?.id_cont,
      provider?.id_contract,
      provider?.identificator_eon,
      provider?.nova_account_id,
      provider?.invoice_id,
      provider?.nume_cont,
      provider?.adresa,
      provider?.adresa_originala,
    ].map((value) => this._normalizeText(value)).filter((value) => value && value.length >= 3);
  }

  _findProviderFinancialSensor(provider, sensorKind) {
    const states = Object.values(this._hass?.states || {});
    const providerKey = this._providerKey(provider);
    const idCont = String(provider?.id_cont ?? "").trim();
    const idContract = String(provider?.id_contract ?? "").trim();
    const terms = this._providerIdentityTerms(provider);
    let best = null;
    let bestScore = -1;

    for (const stateObj of states) {
      if (!stateObj?.entity_id?.startsWith("sensor.")) continue;
      const entityId = String(stateObj.entity_id || "").toLowerCase();
      if (!entityId.includes(sensorKind)) continue;
      const attrs = stateObj.attributes || {};
      const friendly = this._entityFriendlyText(stateObj);
      let score = 0;

      if (providerKey && entityId.includes(providerKey)) score += 120;
      if (providerKey && friendly.includes(providerKey.replace(/_/g, " "))) score += 60;
      if (idCont && String(attrs.id_cont ?? "").trim() === idCont) score += 220;
      if (idContract && String(attrs.id_contract ?? "").trim() === idContract) score += 180;
      if (terms.length && this._textMatchesAny(`${entityId} ${friendly}`, terms)) score += 60;
      if (stateObj.state !== "unknown" && stateObj.state !== "unavailable" && stateObj.state !== "") score += 40;

      if (score > bestScore) {
        best = stateObj;
        bestScore = score;
      }
    }

    return bestScore >= 120 ? best : null;
  }

  _rateSensorLabel(stateObj) {
    if (!stateObj || stateObj.state === "unknown" || stateObj.state === "unavailable" || stateObj.state === "") return "";
    const value = this._num(stateObj.state);
    if (!Number.isFinite(value) || value <= 0) return "";
    const unit = stateObj.attributes?.unit_of_measurement || "RON/kWh";
    const formatted = new Intl.NumberFormat("ro-RO", { maximumFractionDigits: 4 }).format(value);
    return `${formatted} ${unit}`;
  }

  _providerShortName(provider) {
    const name = this._providerName(provider);
    const normalized = this._normalizeText(name);
    if (normalized.includes("hidroelectrica")) return "Hidro";
    if (normalized.includes("nova")) return "Nova";
    if (normalized.includes("e.on") || normalized.includes("eon")) return "E.ON";
    if (normalized.includes("engie")) return "ENGIE";
    if (normalized.includes("electrica")) return "Electrica";
    if (normalized.includes("apa") || normalized.includes("canal")) return "Apă";
    return String(name || "").split(/\s+/).slice(0, 2).join(" ") || "Furnizor";
  }

  _providerUtilityShort(provider) {
    const utility = this._providerUtilityType(provider);
    const normalized = this._normalizeText(utility);
    if (normalized.includes("gaz")) return "gaz";
    if (normalized.includes("energie") || normalized.includes("electric") || normalized.includes("curent")) return "curent";
    if (normalized.includes("apa") || normalized.includes("canal")) return "apă";
    if (normalized.includes("salubritate")) return "salubritate";
    if (normalized.includes("telecom")) return "telecom";
    return utility ? utility.toLowerCase() : "utilitate";
  }

  _providerFinancialChips(provider) {
    const costSensor = this._findProviderFinancialSensor(provider, "cost_mediu_unitate_ultima_factura");
    const prosumerSensor = this._findProviderFinancialSensor(provider, "pret_mediu_energie_prosumator_ultima_factura");
    const chips = [];
    const cost = this._rateSensorLabel(costSensor);
    const prosumer = this._rateSensorLabel(prosumerSensor);
    const providerLabel = this._providerShortName(provider);
    const utilityLabel = this._providerUtilityShort(provider);

    if (cost) {
      chips.push({
        label: `${providerLabel} ${utilityLabel}`,
        value: cost,
        icon: utilityLabel === "gaz" ? "mdi:fire" : utilityLabel === "apă" ? "mdi:water" : "mdi:transmission-tower",
        title: `${this._providerName(provider)} - cost mediu ${utilityLabel}`
      });
    }
    if (prosumer) {
      chips.push({
        label: `${providerLabel} prosumator`,
        value: prosumer,
        icon: "mdi:solar-power-variant",
        title: `${this._providerName(provider)} - preț mediu energie prosumator`
      });
    }
    return chips;
  }

  _renderFinancialChipsForProviders(providers) {
    const chips = [];
    for (const provider of providers || []) {
      const providerChips = this._providerFinancialChips(provider);
      for (const chip of providerChips) {
        chips.push(`
          <span class="financial-chip" title="${this._escape(chip.title || `${this._providerName(provider)} - ${chip.label}`)}">
            <ha-icon icon="${chip.icon}"></ha-icon>
            <span>${this._escape(chip.label)}</span>
            <strong>${this._escape(chip.value)}</strong>
          </span>
        `);
      }
    }
    return chips.length ? `<div class="financial-chip-row">${chips.join("")}</div>` : "";
  }

  _status(provider) {
    const raw = String(provider?.status || provider?.payment_status || provider?.status_raw || "unknown").toLowerCase();
    if (["paid", "platita", "plătită", "credit"].includes(raw)) return raw === "credit" ? "credit" : "paid";
    if (["unpaid", "neplatita", "neplătită", "restanta", "restanță", "de_plata"].includes(raw)) return "unpaid";
    return "unknown";
  }

  _statusLabel(status) {
    if (status === "paid") return "Plătită";
    if (status === "unpaid") return "De plată";
    if (status === "credit") return "Credit";
    return "Necunoscut";
  }

  _providerInvoiceAmount(provider) {
    return provider?.amount ?? provider?.suma ?? provider?.valoare ?? provider?.total ?? null;
  }

  _isRerVestProvider(provider) {
    const key = String(provider?.furnizor || provider?.provider || provider?.provider_key || provider?.platform || "").toLowerCase();
    const label = String(provider?.furnizor_label || provider?.supplier || provider?.name || "").toLowerCase();
    return key === "rervest" || key === "rer_vest" || label.includes("rer vest");
  }

  _providerAmount(provider) {
    const invoiceAmount = this._num(this._providerInvoiceAmount(provider));
    const unpaidTotal = this._providerUnpaidTotal(provider);

    // RER Vest poate avea mai multe facturi neachitate pentru același loc de consum,
    // dar rândul principal rămâne ultima factură. În acest caz afișăm totalul de plată,
    // nu doar valoarea ultimei facturi.
    if (this._isRerVestProvider(provider) && this._status(provider) === "unpaid" && unpaidTotal > invoiceAmount) {
      return unpaidTotal;
    }

    return this._providerInvoiceAmount(provider) ?? provider?.unpaid_amount ?? null;
  }

  _providerUnpaidCount(provider) {
    if (this._status(provider) !== "unpaid") return 0;

    const providerKey = String(provider?.furnizor || provider?.furnizor_key || provider?.provider || provider?.provider_key || "").toLowerCase();
    const providerLabel = String(provider?.furnizor_label || provider?.supplier || provider?.name || "").toLowerCase();

    // DIGI poate întoarce în aceeași structură lista facturilor neplătite de pe
    // tot contul, chiar dacă în dashboard afișăm deja rânduri separate pe
    // servicii/locuri de consum. În antet trebuie numărate rândurile afișate,
    // nu lista comună din payload, altfel apare 4 neplătite pentru 2 rânduri.
    if (providerKey === "digi" || providerLabel === "digi") {
      return 1;
    }

    const explicitCount = this._num(provider?.unpaid_count);
    if (Number.isFinite(explicitCount) && explicitCount > 0) {
      return Math.round(explicitCount);
    }

    if (Array.isArray(provider?.unpaid_invoice_ids) && provider.unpaid_invoice_ids.length > 0) {
      return provider.unpaid_invoice_ids.length;
    }

    const invoiceAmount = this._num(this._providerInvoiceAmount(provider));
    const unpaidTotal = this._providerUnpaidTotal(provider);
    if (this._isRerVestProvider(provider) && invoiceAmount > 0 && unpaidTotal > invoiceAmount) {
      return 2;
    }

    return 1;
  }

  _providerUnpaidTotal(provider) {
    if (this._status(provider) !== "unpaid") return 0;
    const total = this._num(provider?.unpaid_total ?? provider?.unpaid_amount);
    if (total > 0) return total;
    return this._num(this._providerInvoiceAmount(provider));
  }

  _providerDue(provider) {
    return provider?.due_date || provider?.scadenta || provider?.data_scadenta || provider?.invoice_due_date || null;
  }

  _isDigiProvider(provider) {
    return this._providerKey(provider) === "digi" || this._normalizeText(provider?.furnizor || provider?.furnizor_label || provider?.provider || "").includes("digi");
  }

  _looksLikeDigiInternalInvoiceId(provider, value) {
    if (!this._isDigiProvider(provider)) return false;
    const text = String(value ?? "").trim();
    if (!/^\d{8,}$/.test(text)) return false;
    const internalIds = [provider?.invoice_id, provider?.id_factura, provider?.last_invoice_id, provider?.ultima_factura_id, provider?.id_ultima_factura]
      .map((item) => String(item ?? "").trim())
      .filter(Boolean);
    return internalIds.includes(text);
  }

  _cleanDigiInvoiceTitle(provider, title, documentNumber = "") {
    let text = String(title ?? "").trim();
    if (!text || !this._isDigiProvider(provider)) return text;
    text = text.replace(/\s+/g, " ");
    text = text.replace(/\s*[·-]\s*factura\s+\d{8,}\b/gi, "").trim();
    if (/^factura\s+\d{8,}$/i.test(text)) return documentNumber ? `Factura ${documentNumber}` : "Factură Digi";
    return text || (documentNumber ? `Factura ${documentNumber}` : "Factură Digi");
  }

  _providerDocumentNumber(provider) {
    const candidates = [
      provider?.numar_factura,
      provider?.nr_factura,
      provider?.invoice_number,
      provider?.invoice_no,
      provider?.document_number,
      provider?.document_no,
      provider?.numar_document,
      provider?.nr_document,
      provider?.nr_doc,
      provider?.serie_numar,
      provider?.invoice_id,
      provider?.id_factura,
      provider?.last_invoice_id,
      provider?.ultima_factura_id,
      provider?.id_ultima_factura,
      Array.isArray(provider?.invoice_ids) ? provider.invoice_ids[0] : null,
      Array.isArray(provider?.unpaid_invoice_ids) ? provider.unpaid_invoice_ids[0] : null,
    ];

    for (const value of candidates) {
      const text = String(value ?? "").trim();
      if (!text || ["-", "—", "none", "null", "undefined"].includes(text.toLowerCase())) continue;
      if (this._looksLikeDigiInternalInvoiceId(provider, text)) continue;
      return text.replace(/\s+/g, " ");
    }
    return "";
  }

  _providerInvoice(provider) {
    const titleCandidates = [
      provider?.invoice_title,
      provider?.last_invoice,
      provider?.ultima_factura,
    ];
    const title = titleCandidates
      .map((value) => String(value ?? "").trim())
      .find((value) => value && !["-", "—", "none", "null", "undefined"].includes(value.toLowerCase()));
    const documentNumber = this._providerDocumentNumber(provider);

    const displayTitle = this._cleanDigiInvoiceTitle(provider, title, documentNumber);

    if (displayTitle && displayTitle !== "Ultima factură") {
      const normalizedTitle = this._normalizeText(displayTitle);
      const normalizedDocument = this._normalizeText(documentNumber);
      if (documentNumber && normalizedTitle === normalizedDocument) return `Factura ${documentNumber}`;
      if (documentNumber && !normalizedTitle.includes(normalizedDocument) && !normalizedTitle.includes("factura")) {
        return `${displayTitle} · factura ${documentNumber}`;
      }
      return displayTitle;
    }

    if (documentNumber) return `Factura ${documentNumber}`;
    return "Factura curentă";
  }

  _allProviders(locations) {
    return locations.flatMap((location) => {
      const providers = Array.isArray(location?.furnizori) ? location.furnizori : [];
      return providers.map((provider) => ({ location, provider }));
    });
  }

  _soonProviders(locations) {
    return this._allProviders(locations)
      .filter(({ provider }) => this._status(provider) === "unpaid")
      .map((item) => ({ ...item, days: this._daysUntil(this._providerDue(item.provider)) }))
      .filter((item) => item.days !== null)
      .sort((a, b) => a.days - b.days)
      .slice(0, 6);
  }

  _renderHero(attrs) {
    const unpaid = this._num(attrs.numar_neplatite);
    const totalUnpaid = attrs.total_neplatit_formatat || this._money(attrs.total_neplatit, attrs.moneda || "RON");
    const statusClass = unpaid > 0 ? "attention" : "ok";
    const backButton = this._backButtonPreferences();
    return `
      <section class="hero">
        <div class="hero-content">
          ${backButton.enabled ? `<button class="dashboard-back-button" type="button" data-dashboard-back title="Mergi la ${this._escape(backButton.target)}"><ha-icon icon="mdi:arrow-left"></ha-icon><span>${this._escape(backButton.label)}</span></button>` : ""}
          <a class="forge-lockup" href="https://haforgelabs.ro" target="_blank" rel="noopener noreferrer" title="Deschide site-ul HAForge Labs"><img class="forge-logo" src="/utilitati_romania/haforge-logo.png" alt="HAForge Labs"><span>HAForge Labs</span><small class="forge-version">Card v${UTILITATI_ROMANIA_FRONTEND_VERSION}</small></a>
          <div class="brand-row">
            <img class="utility-logo" src="/utilitati_romania/logo.png" alt="Utilități România">
            <div class="brand-meta">
              <h1>Utilități România</h1>
            </div>
          </div>
          <p>Toate facturile, indexurile și locurile de consum într-un panou unic, construit pentru verificare rapidă și administrare clară.</p>
        </div>
        <div class="hero-card ${statusClass}">
          <span class="hero-card-label">Total de plată</span>
          <strong>${this._escape(totalUnpaid)}</strong>
          <small>${unpaid ? `${unpaid} facturi necesită atenție` : "Nu sunt facturi restante în datele agregate"}</small>
        </div>
      </section>
    `;
  }

  _renderMetrics(attrs, locations) {
    const providersCount = attrs.numar_facturi ?? this._allProviders(locations).length;
    const unpaid = attrs.numar_neplatite ?? 0;
    return `
      <section class="metrics">
        ${this._metric("Locații", locations.length, "mdi:map-marker-radius")}
        ${this._metric("Facturi", providersCount, "mdi:file-document-outline")}
        ${this._metric("Neplătite", unpaid, "mdi:alert-circle", this._num(unpaid) > 0 ? "warn" : "")}
        ${this._metric("Plătite", attrs.numar_platite ?? 0, "mdi:check-circle", "ok")}
      </section>
    `;
  }

  _metric(label, value, icon, tone = "") {
    return `
      <article class="metric ${tone}">
        <ha-icon icon="${icon}"></ha-icon>
        <span>${this._escape(label)}</span>
        <strong>${this._escape(value)}</strong>
      </article>
    `;
  }

  _renderTabs() {
    const tabs = [
      ["overview", "Prezentare", "mdi:view-dashboard"],
      ["invoices", "Facturi", "mdi:file-document-outline"],
      ["readings", "Indexuri", "mdi:gauge"],
      ["license", "Licență", "mdi:shield-check"],
      ["contact", "Contact", "mdi:email-outline"],
      ["settings", "Setări", "mdi:cog-outline"],
      ["diagnostics", "Diagnostic", "mdi:tools"],
    ];
    return `<nav class="tabs">${tabs.map(([id, label, icon]) => `
      <button class="tab ${this._activeTab === id ? "active" : ""}" data-tab="${id}">
        <ha-icon icon="${icon}"></ha-icon><span>${label}</span>
      </button>`).join("")}</nav>`;
  }

  _renderOverview(attrs, locations) {
    const soon = this._soonProviders(locations);
    const displayLocations = this._displayLocations(locations);
    return `
      <div class="grid two">
        <section class="panel-card">
          <div class="card-head"><div><span class="eyebrow">scadențe</span><h2>Următoarele facturi</h2></div></div>
          ${soon.length ? soon.map((item) => this._dueItem(item)).join("") : `<div class="empty">Nu există scadențe apropiate în datele disponibile.</div>`}
        </section>
        <section class="panel-card">
          <div class="card-head"><div><span class="eyebrow">locații</span><h2>Sumar pe locuri de consum</h2></div></div>
          ${displayLocations.length ? displayLocations.map((location, index) => this._locationCompact(location, index)).join("") : `<div class="empty">Nu există încă date agregate. Verifică dacă există cel puțin un furnizor configurat.</div>`}
        </section>
      </div>
      <section class="panel-card wide">
        <div class="card-head"><div><span class="eyebrow">status</span><h2>Imagine generală</h2></div></div>
        <div class="summary-strip">
          <div><strong>${this._escape(attrs.numar_facturi ?? 0)}</strong><span>facturi / furnizori</span></div>
          <div><strong>${this._escape(attrs.numar_necunoscute ?? 0)}</strong><span>status necunoscut</span></div>
          <div><strong>${this._escape(attrs.ultima_eroare || "fără erori")}</strong><span>ultima eroare agregare</span></div>
        </div>
      </section>
    `;
  }

  _dueItem({ location, provider, days }) {
    const dueTone = days < 0 ? "late" : days <= 3 ? "soon" : "normal";
    const dueText = days < 0 ? `întârziată cu ${Math.abs(days)} zile` : days === 0 ? "scadentă azi" : `${days} zile rămase`;
    return `
      <article class="due ${dueTone}">
        <div><strong>${this._escape(this._providerName(provider))}</strong><span>${this._escape(this._billingDisplayName(location, provider))}</span></div>
        <div class="due-right"><b>${this._escape(this._money(this._providerAmount(provider), provider?.currency || "RON"))}</b><small>${this._escape(dueText)}</small></div>
      </article>
    `;
  }

  _locationCompact(location, index) {
    const providers = Array.isArray(location?.furnizori) ? location.furnizori : [];
    const unpaid = providers.reduce((sum, provider) => sum + (this._status(provider) === "unpaid" ? Math.max(1, this._num(provider?.unpaid_count || 1)) : 0), 0);
    const financialChips = this._renderFinancialChipsForProviders(providers);
    return `
      <article class="location-compact ${financialChips ? "with-financial" : ""}">
        <div class="location-icon">${index + 1}</div>
        <div class="location-compact-main">
          <strong>${this._escape(this._displayLocationName(location))}</strong>
          <span>${providers.length} furnizori · ${unpaid} neplătite</span>
          ${financialChips}
        </div>
        <b>${this._escape(location?.total_neplatit_formatat || this._money(location?.total_neplatit, "RON"))}</b>
      </article>
    `;
  }


  _invoiceGroupingStorageKey() {
    return "utilitati_romania_panel_invoice_grouping";
  }

  _invoiceGroupingOptions() {
    return [
      { value: "location", label: "Locație / cont" },
      { value: "due_date", label: "Scadență" },
      { value: "urgency", label: "Urgență" },
      { value: "status", label: "Status plată" },
      { value: "provider", label: "Furnizor" },
      { value: "amount", label: "Valoare" },
    ];
  }

  _invoiceFilterStorageKey() {
    return "utilitati_romania_panel_invoice_filter";
  }

  _invoiceFilterOptions() {
    return [
      { value: "all", label: "Toate" },
      { value: "unpaid", label: "Neplătite" },
      { value: "paid", label: "Plătite" },
      { value: "due_5", label: "Scadente curând" },
      { value: "unknown", label: "Necunoscute" },
    ];
  }

  _loadInvoiceFilterPreference() {
    try { return window.localStorage?.getItem(this._invoiceFilterStorageKey()) || ""; } catch (_err) { return ""; }
  }

  _saveInvoiceFilterPreference(value) {
    try { window.localStorage?.setItem(this._invoiceFilterStorageKey(), value); } catch (_err) {}
  }

  _isValidInvoiceFilter(value) {
    return this._invoiceFilterOptions().some((option) => option.value === value);
  }

  _setInvoiceFilter(value) {
    this._invoiceFilter = this._isValidInvoiceFilter(value) ? value : "all";
    this._saveInvoiceFilterPreference(this._invoiceFilter);
  }

  _filterInvoiceEntries(entries, filter) {
    const activeFilter = this._isValidInvoiceFilter(filter) ? filter : "all";
    if (activeFilter === "all") return entries || [];
    return (entries || []).filter((entry) => {
      if (activeFilter === "unpaid") return entry.status === "unpaid";
      if (activeFilter === "paid") return entry.status === "paid";
      if (activeFilter === "unknown") return entry.status === "unknown";
      if (activeFilter === "due_5") {
        const days = this._daysUntil(entry.dueDate);
        return entry.status === "unpaid" && Number.isFinite(days) && days <= 5;
      }
      return true;
    });
  }

  _loadInvoiceGroupingPreference() {
    try { return window.localStorage?.getItem(this._invoiceGroupingStorageKey()) || ""; } catch (_err) { return ""; }
  }

  _saveInvoiceGroupingPreference(value) {
    try { window.localStorage?.setItem(this._invoiceGroupingStorageKey(), value); } catch (_err) {}
  }

  _isValidInvoiceGrouping(value) {
    return this._invoiceGroupingOptions().some((option) => option.value === value);
  }

  _setInvoiceGrouping(value) {
    this._invoiceGrouping = this._isValidInvoiceGrouping(value) ? value : "location";
    this._saveInvoiceGroupingPreference(this._invoiceGrouping);
  }

  _daysPastDue(value) {
    const days = this._daysUntil(value);
    return days !== null && days < 0 ? Math.abs(days) : null;
  }

  _collectInvoiceEntries(locations) {
    const entries = [];
    for (const location of locations || []) {
      const providers = Array.isArray(location?.furnizori) ? location.furnizori : [];
      providers.forEach((provider, index) => {
        entries.push({
          location,
          provider,
          index,
          status: this._status(provider),
          supplier: this._providerName(provider),
          dueDate: this._providerDue(provider),
          amount: this._num(this._providerAmount(provider)),
        });
      });
    }
    return entries;
  }

  _invoiceDueTime(entry) {
    const parsed = this._parseDateLike(entry?.dueDate);
    return parsed ? parsed.getTime() : Number.POSITIVE_INFINITY;
  }

  _compareInvoiceEntries(a, b) {
    const dueDiff = this._invoiceDueTime(a) - this._invoiceDueTime(b);
    if (Number.isFinite(dueDiff) && dueDiff !== 0) return dueDiff;
    const statusOrder = { unpaid: 0, unknown: 1, credit: 2, paid: 3 };
    const statusDiff = (statusOrder[a.status] ?? 9) - (statusOrder[b.status] ?? 9);
    if (statusDiff !== 0) return statusDiff;
    const supplierDiff = String(a.supplier || "").localeCompare(String(b.supplier || ""), "ro");
    if (supplierDiff !== 0) return supplierDiff;
    return String(a.provider?.invoice_title || "").localeCompare(String(b.provider?.invoice_title || ""), "ro");
  }

  _invoiceStatusGroup(entry) {
    const status = entry?.status || "unknown";
    const order = { unpaid: 0, paid: 1, credit: 2, unknown: 3 };
    return { key: `status_${status}`, title: this._statusLabel(status), order: order[status] ?? 9 };
  }

  _invoiceProviderGroup(entry) {
    const supplier = entry?.supplier || "Furnizor";
    return { key: `provider_${this._normalizeText(supplier) || "furnizor"}`, title: supplier, order: 0 };
  }

  _invoiceUrgencyGroup(entry) {
    const past = this._daysPastDue(entry?.dueDate);
    const until = this._daysUntil(entry?.dueDate);
    if (entry?.status === "unpaid" && Number.isFinite(past) && past > 0) return { key: "urgency_overdue", title: "Depășite", order: 0 };
    if (entry?.status === "unpaid" && Number.isFinite(until) && until >= 0 && until <= 5) return { key: "urgency_soon", title: "Scadente în următoarele 5 zile", order: 1 };
    if (entry?.status === "unpaid") return { key: "urgency_unpaid", title: "Neplătite", order: 2 };
    if (entry?.status === "paid") return { key: "urgency_paid", title: "Plătite", order: 3 };
    if (entry?.status === "credit") return { key: "urgency_credit", title: "Credit", order: 4 };
    return { key: "urgency_unknown", title: "Necunoscute", order: 5 };
  }

  _invoiceAmountGroup(entry) {
    const amount = this._num(entry?.amount);
    if (!amount) return { key: "amount_none", title: "Fără valoare", order: 99 };
    if (amount >= 500) return { key: "amount_500", title: "Peste 500 lei", order: 0 };
    if (amount >= 200) return { key: "amount_200_499", title: "200–499 lei", order: 1 };
    if (amount >= 100) return { key: "amount_100_199", title: "100–199 lei", order: 2 };
    return { key: "amount_under_100", title: "Sub 100 lei", order: 3 };
  }

  _invoiceDueDateGroup(entry) {
    const date = this._parseDateLike(entry?.dueDate);
    if (!date) return { key: "due_none", title: "Fără scadență", order: 50 };
    const past = this._daysPastDue(entry.dueDate);
    const until = this._daysUntil(entry.dueDate);
    if (entry.status === "unpaid" && Number.isFinite(past) && past > 0) return { key: "due_overdue", title: "Depășite", order: 0 };
    if (Number.isFinite(until) && until === 0) return { key: "due_today", title: "Scadente astăzi", order: 1 };
    if (Number.isFinite(until) && until > 0 && until <= 5) return { key: "due_soon", title: "Următoarele 5 zile", order: 2 };
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, "0");
    let title = `${month}.${year}`;
    try { title = new Intl.DateTimeFormat("ro-RO", { month: "long", year: "numeric" }).format(date); } catch (_err) {}
    return { key: `due_${year}_${month}`, title: title.charAt(0).toUpperCase() + title.slice(1), order: 10 + year * 12 + date.getMonth() };
  }

  _invoiceGroupForEntry(entry, grouping) {
    if (grouping === "status") return this._invoiceStatusGroup(entry);
    if (grouping === "provider") return this._invoiceProviderGroup(entry);
    if (grouping === "due_date") return this._invoiceDueDateGroup(entry);
    if (grouping === "urgency") return this._invoiceUrgencyGroup(entry);
    if (grouping === "amount") return this._invoiceAmountGroup(entry);
    const title = this._billingDisplayName(entry?.location, entry?.provider);
    const key = this._normalizeText(title) || this._displayLocationGroupKey(entry?.location);
    return { key: `location_${key}`, title, order: 0 };
  }

  _buildInvoiceGroups(entries, grouping) {
    const groups = new Map();
    for (const entry of entries || []) {
      const info = this._invoiceGroupForEntry(entry, grouping);
      if (!groups.has(info.key)) groups.set(info.key, { key: info.key, title: info.title, order: info.order, entries: [] });
      groups.get(info.key).entries.push(entry);
    }
    return Array.from(groups.values()).map((group) => ({
      ...group,
      entries: group.entries.sort((a, b) => {
        if (grouping === "amount") {
          const amountDiff = this._num(b.amount) - this._num(a.amount);
          if (amountDiff !== 0) return amountDiff;
        }
        return this._compareInvoiceEntries(a, b);
      }),
    })).sort((a, b) => {
      const orderDiff = (a.order ?? 0) - (b.order ?? 0);
      if (orderDiff !== 0) return orderDiff;
      return String(a.title || "").localeCompare(String(b.title || ""), "ro");
    });
  }

  _invoiceGroupSummary(entries) {
    const providers = (entries || []).map((entry) => entry.provider).filter(Boolean);
    const paid = providers.filter((provider) => this._status(provider) === "paid").length;
    const unpaid = providers.reduce((sum, provider) => sum + this._providerUnpaidCount(provider), 0);
    const credit = providers.filter((provider) => this._status(provider) === "credit").length;
    const totalUnpaid = providers.reduce((sum, provider) => sum + this._providerUnpaidTotal(provider), 0);
    const parts = [];
    if (unpaid) parts.push(this._pluralRo(unpaid, "neplătită", "neplătite"));
    if (paid) parts.push(this._pluralRo(paid, "plătită", "plătite"));
    if (credit) parts.push(`${credit} credit`);
    if (totalUnpaid > 0) parts.push(`total neplătit ${this._money(totalUnpaid, "RON")}`);
    return parts.join(" · ") || `${entries.length} facturi`;
  }

  _renderInvoiceToolbar(grouping, count, totalCount, filter) {
    const groupOptions = this._invoiceGroupingOptions().map((option) => `<option value="${this._escape(option.value)}" ${option.value === grouping ? "selected" : ""}>${this._escape(option.label)}</option>`).join("");
    const filterOptions = this._invoiceFilterOptions().map((option) => `<option value="${this._escape(option.value)}" ${option.value === filter ? "selected" : ""}>${this._escape(option.label)}</option>`).join("");
    const counter = filter && filter !== "all" ? `${count} din ${totalCount} facturi` : `${count} ${count === 1 ? "factură" : "facturi"}`;
    return `<section class="invoice-toolbar panel-card compact"><label for="ur-panel-invoice-filter">Filtru</label><select id="ur-panel-invoice-filter" data-invoice-filter>${filterOptions}</select><label for="ur-panel-invoice-grouping">Grupare</label><select id="ur-panel-invoice-grouping" data-invoice-grouping>${groupOptions}</select><span>${this._escape(counter)}</span></section>`;
  }

  _findRefreshButton(provider) {
    const states = Object.values(this._hass?.states || {});
    const providerKey = this._providerKey(provider);
    const providerName = this._normalizeText(this._providerName(provider));
    const idCont = String(provider?.id_cont ?? "").trim();
    let best = null;
    let bestScore = -1;
    for (const stateObj of states) {
      if (!stateObj?.entity_id?.startsWith("button.")) continue;
      const text = this._entityFriendlyText(stateObj);
      const entityId = String(stateObj.entity_id || "").toLowerCase();
      if (!text.includes("actualizeaza") && !entityId.includes("actualizare")) continue;
      let score = 0;
      if (providerKey && entityId.includes(providerKey)) score += 120;
      if (providerKey && text.includes(providerKey.replace(/_/g, " "))) score += 80;
      if (providerName && text.includes(providerName)) score += 80;
      if (idCont && (entityId.includes(idCont) || String(stateObj.attributes?.id_cont ?? "") === idCont)) score += 40;
      if (score > bestScore) {
        best = stateObj;
        bestScore = score;
      }
    }
    return bestScore >= 70 ? best.entity_id : null;
  }

  _renderRefreshButton(provider) {
    const entityId = this._findRefreshButton(provider);
    const key = `refresh__${entityId || this._providerKey(provider)}__${provider?.id_cont || ""}`;
    const action = this._actions.get(key);
    if (!entityId) return `<button class="row-action disabled" disabled title="Butonul de actualizare nu a fost găsit"><ha-icon icon="mdi:refresh-off"></ha-icon></button>`;
    const message = action?.status === "ok" ? `<small class="refresh-message ok">Actualizat</small>` : action?.status === "error" ? `<small class="refresh-message error">Eroare</small>` : "";
    return `<div class="refresh-wrap"><button class="row-action ${action?.status === "busy" ? "busy" : ""}" data-refresh-entity="${this._escape(entityId)}" data-action-key="${this._escape(key)}" title="Actualizează acest furnizor" aria-label="Actualizează acest furnizor" ${action?.status === "busy" ? "disabled" : ""}><ha-icon icon="mdi:refresh"></ha-icon></button>${message}</div>`;
  }

  _providerAppLabel(provider) {
    const key = this._providerKey(provider);
    const labels = {
      digi: "App. Digi",
      eon: "App. E.ON",
      hidroelectrica: "App. Hidroelectrica",
      myelectrica: "App. myElectrica",
      nova: "App. Nova",
      ebloc: "App. e-bloc",
      orange: "App. Orange",
      comprest: "Portal Comprest",
      apa_oradea: "Portal Apă Oradea",
      apa_galati: "Portal Apă Canal Galați",
      hidro_prahova: "Portal Hidro Prahova",
    };
    return labels[key] || "";
  }

  _renderOpenProviderButton(provider) {
    const providerKey = this._providerKey(provider);
    const label = this._providerAppLabel(provider);
    if (!providerKey || !label) return "";
    const action = this._actions.get(`open_provider__${providerKey}`);
    const busy = action?.status === "busy";
    return `
      <button class="provider-app-action ${busy ? "busy" : ""}" data-open-provider="${this._escape(providerKey)}" title="Deschide ${this._escape(label)}" aria-label="Deschide ${this._escape(label)}" ${busy ? "disabled" : ""}>
        <ha-icon icon="mdi:open-in-app"></ha-icon>
        <span>${this._escape(label)}</span>
      </button>
    `;
  }

  _renderInvoices(locations) {
    if (!locations.length) return `<section class="panel-card"><div class="empty">Nu există facturi în senzorul agregat.</div></section>`;
    const grouping = this._isValidInvoiceGrouping(this._invoiceGrouping) ? this._invoiceGrouping : "location";
    const filter = this._isValidInvoiceFilter(this._invoiceFilter) ? this._invoiceFilter : "all";
    const allEntries = this._collectInvoiceEntries(locations);
    const entries = this._filterInvoiceEntries(allEntries, filter);
    const groups = this._buildInvoiceGroups(entries, grouping);
    return `
      ${this._renderInvoiceToolbar(grouping, entries.length, allEntries.length, filter)}
      ${entries.length ? groups.map((group) => `
        <section class="panel-card location-card">
          <div class="location-title static">
            <div><span class="eyebrow">${this._escape(grouping === "location" ? "loc de consum" : "grupare")}</span><h2>${this._escape(group.title)}</h2></div>
            <div class="location-total"><strong>${this._escape(this._invoiceGroupSummary(group.entries))}</strong></div>
          </div>
          <div class="invoice-list">${group.entries.map((entry) => this._invoiceRow(entry.location, entry.provider, grouping)).join("")}</div>
        </section>
      `).join("") : `<section class="panel-card"><div class="empty">Nu există facturi pentru filtrul selectat.</div></section>`}
    `;
  }

  _invoiceKey(location, provider) {
    return this._makeKey("invoice", location?.locatie_cheie || location?.eticheta_locatie || "locatie", this._providerName(provider), this._providerInvoice(provider), this._providerDue(provider), this._providerAmount(provider));
  }

  _invoiceRow(location, provider, grouping = "location") {
    const status = this._status(provider);
    const due = this._providerDue(provider);
    const days = this._daysUntil(due);
    const warning = status === "unpaid" && days !== null && days <= 5;
    const key = this._invoiceKey(location, provider);
    const expanded = this._expandedInvoices.has(key);
    const utilityType = this._providerUtilityType(provider);
    const utilityLine = utilityType ? `<span class="invoice-utility">${this._escape(utilityType)}</span>` : "";
    const documentNumber = this._providerDocumentNumber(provider);
    const documentMeta = documentNumber ? `<div class="invoice-meta invoice-document-meta"><span>Document</span><strong>${this._escape(documentNumber)}</strong></div>` : "";
    const displayName = this._providerDisplayName(location, provider);
    const locationLine = this._invoiceLocationLine(location, provider, displayName, grouping);
    return `
      <article class="invoice-row ${status} ${warning ? "warning" : ""} ${expanded ? "expanded" : ""}">
        <div class="provider-badge">${this._escape(this._providerName(provider).slice(0, 2).toUpperCase())}</div>
        <div class="invoice-main"><strong>${this._escape(displayName)}</strong>${locationLine}<span>${this._escape(this._providerInvoice(provider))}</span>${utilityLine}</div>
        <div class="invoice-quick"><strong>${this._escape(this._money(this._providerAmount(provider), provider?.currency || "RON"))}</strong><span class="pill ${status}">${this._escape(this._statusLabel(status))}</span></div>
        <button class="invoice-toggle" data-toggle-invoice="${this._escape(key)}" title="Detalii factură" aria-label="Detalii factură"><ha-icon icon="${expanded ? "mdi:chevron-up" : "mdi:chevron-down"}"></ha-icon></button>
        <div class="invoice-details">
          ${documentMeta}
          <div class="invoice-meta"><span>Scadență</span><strong>${this._escape(this._date(due))}</strong></div>
          ${utilityType ? `<div class="invoice-meta"><span>Utilitate</span><strong>${this._escape(utilityType)}</strong></div>` : ""}
          <div class="invoice-meta amount"><span>Valoare</span><strong>${this._escape(this._money(this._providerAmount(provider), provider?.currency || "RON"))}</strong></div>
          <span class="pill ${status}">${this._escape(this._statusLabel(status))}</span>
          <div class="invoice-actions">
            ${this._renderOpenProviderButton(provider)}
            ${this._renderRefreshButton(provider)}
          </div>
        </div>
      </article>
    `;
  }

  _readingTerms(location, provider) {
    const values = [location?.eticheta_locatie, provider?.nume_cont, provider?.adresa_originala, provider?.invoice_title, provider?.id_cont, provider?.id_contract];
    const normalized = values.map((value) => this._normalizeText(value)).filter(Boolean);
    const extra = [];
    for (const value of normalized) {
      const noNumbers = value.replace(/\b\d+\b/g, " ").replace(/\s+/g, " ").trim();
      if (noNumbers && noNumbers !== value) extra.push(noNumbers);
    }
    return Array.from(new Set([...normalized, ...extra])).filter((value) => value.length >= 3);
  }

  _findReadingSensor(location, provider) {
    const providerKey = this._providerKey(provider);
    const targetIdCont = String(provider?.id_cont ?? "").trim();
    const targetIdContract = String(provider?.id_contract ?? "").trim();
    const terms = this._readingTerms(location, provider);
    const normalizedProvider = providerKey.replace(/_/g, " ");

    if (!providerKey || !["hidroelectrica", "engie", "eon", "myelectrica", "apa_canal", "apa_brasov", "apa_oradea", "apa_galati", "hidro_prahova"].includes(providerKey)) return null;

    const candidates = Object.values(this._hass?.states || {}).filter((stateObj) => {
      if (!stateObj?.entity_id?.startsWith("sensor.")) return false;
      const entityId = stateObj.entity_id;
      const attrs = stateObj.attributes || {};
      const text = this._entityFriendlyText(stateObj);
      const looksLikeReadingSensor = !!(
        entityId.includes("citire_permisa") ||
        entityId.includes("citire_index_permisa") ||
        text.includes("citire permisa") ||
        attrs.inceput_perioada || attrs.sfarsit_perioada || attrs["Perioadă start"] || attrs["Perioadă sfârșit"]
      );
      if (!looksLikeReadingSensor) return false;
      return entityId.includes(providerKey) || text.includes(normalizedProvider);
    });

    let best = null;
    let bestScore = -1;
    for (const stateObj of candidates) {
      const entityId = stateObj.entity_id;
      const attrs = stateObj.attributes || {};
      const text = this._entityFriendlyText(stateObj);
      let score = 50;
      const attrIdCont = String(attrs.id_cont ?? "").trim();
      if (targetIdCont && attrIdCont) {
        if (attrIdCont === targetIdCont) score += 120;
        else continue;
      }
      const attrContract = String(attrs.id_contract ?? attrs.cod_contract ?? "").trim();
      if (targetIdContract && attrContract) {
        if (attrContract === targetIdContract) score += 100;
        else continue;
      }
      const attrAddress = this._normalizeText(attrs.adresa || attrs["Adresă"] || attrs.apartament || attrs.nume_cont || "");
      if (attrAddress && this._textMatchesAny(attrAddress, terms)) score += 70;
      if (this._textMatchesAny(text, terms)) score += 80;
      if (targetIdCont && entityId.includes(targetIdCont)) score += 60;
      if (score > bestScore) {
        best = stateObj;
        bestScore = score;
      }
    }
    return bestScore >= 50 ? best : null;
  }

  _extractWindowInfo(sensorState) {
    if (!sensorState) return { isOpen: false, start: null, end: null };
    const attrs = sensorState.attributes || {};
    const startRaw = attrs.inceput_perioada || attrs["inceput_perioada"] || attrs["Perioadă start"] || attrs.StartDatePAC || attrs.start_date || null;
    const endRaw = attrs.sfarsit_perioada || attrs["sfarsit_perioada"] || attrs["Perioadă sfârșit"] || attrs.EndDatePAC || attrs.end_date || null;
    const startDate = this._parseDateLike(startRaw);
    const endDate = this._parseDateLike(endRaw);
    const today = this._todayDate();
    let openByRange = false;
    if (startDate && endDate) {
      openByRange = today >= new Date(startDate.getFullYear(), startDate.getMonth(), startDate.getDate()) && today <= new Date(endDate.getFullYear(), endDate.getMonth(), endDate.getDate());
    }
    const stateText = this._normalizeText(sensorState.state);
    const truthyState = ["da", "yes", "true", "on", "activ", "disponibil", "permisa", "permis"].includes(stateText);
    return { isOpen: openByRange || truthyState, start: startRaw || null, end: endRaw || null };
  }

  _deriveControlsFromReadingSensor(location, provider, readingSensor) {
    if (!readingSensor) return [];
    const providerKey = this._providerKey(provider);
    const sensorEntityId = readingSensor.entity_id || "";
    const sensorObject = sensorEntityId.replace(/^sensor\./, "");
    const states = this._hass?.states || {};
    const readingText = this._entityFriendlyText(readingSensor);
    const terms = this._readingTerms(location, provider);
    const controls = [];

    if (providerKey === "hidroelectrica") {
      const base = sensorObject.replace(/_citire_permisa$/, "");
      controls.push({ key: `${providerKey}_${provider.id_cont || base}`, label: "Index de transmis", numberEntityId: `number.${base}_index_energie_electrica`, buttonEntityId: `button.${base}_trimite_index`, currentEntityId: `sensor.${base}_index_energie_electrica` });
      return controls;
    }

    if (providerKey === "engie") {
      const base = sensorObject.replace(/_citire_permisa$/, "");
      const targetIdCont = String(provider?.id_cont ?? readingSensor.attributes?.id_cont ?? "").trim();
      const targetIdContract = String(provider?.id_contract ?? readingSensor.attributes?.id_contract ?? "").trim();
      const matchesEngieContext = (stateObj) => {
        const attrs = stateObj?.attributes || {};
        const idCont = String(attrs.id_cont ?? "").trim();
        const idContract = String(attrs.id_contract ?? attrs.cod_contract ?? "").trim();
        if (targetIdCont && idCont && idCont === targetIdCont) return true;
        if (targetIdContract && idContract && idContract === targetIdContract) return true;
        const entityId = String(stateObj?.entity_id || "").toLowerCase();
        const text = this._entityFriendlyText(stateObj);
        return entityId.includes(base) || this._textMatchesAny(text, terms);
      };
      let currentEntity = states[`sensor.${base}_index_contor`] || Object.values(states).find((stateObj) => {
        if (!stateObj?.entity_id?.startsWith("sensor.")) return false;
        const entityId = String(stateObj.entity_id || "").toLowerCase();
        const text = this._entityFriendlyText(stateObj);
        const looksLikeCurrentIndex = entityId.includes("index_contor") || text.includes("index contor");
        const belongsToEngie = entityId.includes("engie") || text.includes("engie") || entityId.includes(base);
        return looksLikeCurrentIndex && belongsToEngie && matchesEngieContext(stateObj);
      }) || null;
      const numberEntity = states[`number.${base}_index_de_transmis`] || Object.values(states).find((stateObj) => {
        if (!stateObj?.entity_id?.startsWith("number.")) return false;
        const entityId = String(stateObj.entity_id || "").toLowerCase();
        const text = this._entityFriendlyText(stateObj);
        const looksLikeInput = entityId.includes("index_de_transmis") || text.includes("index de transmis");
        const belongsToEngie = entityId.includes("engie") || text.includes("engie") || entityId.includes(base);
        return looksLikeInput && belongsToEngie && matchesEngieContext(stateObj);
      }) || null;
      const buttonEntity = states[`button.${base}_trimite_index`] || Object.values(states).find((stateObj) => {
        if (!stateObj?.entity_id?.startsWith("button.")) return false;
        const entityId = String(stateObj.entity_id || "").toLowerCase();
        const text = this._entityFriendlyText(stateObj);
        const looksLikeSubmit = entityId.includes("trimite_index") || text.includes("trimite index");
        const belongsToEngie = entityId.includes("engie") || text.includes("engie") || entityId.includes(base);
        return looksLikeSubmit && belongsToEngie && matchesEngieContext(stateObj);
      }) || null;
      if (numberEntity && buttonEntity) {
        controls.push({ key: `${providerKey}_${provider.id_cont || targetIdCont || base}`, providerKey, label: "Index de transmis", numberEntityId: numberEntity.entity_id, buttonEntityId: buttonEntity.entity_id, currentEntityId: currentEntity?.entity_id || null });
      } else if (currentEntity) {
        controls.push({ key: `${providerKey}_${provider.id_cont || targetIdCont || base}_current`, label: "Index curent", numberEntityId: null, buttonEntityId: null, currentEntityId: currentEntity.entity_id });
      }
      return controls;
    }

    if (providerKey === "eon") {
      const base = sensorObject.replace(/_citire_permisa$/, "");
      const idCont = String(provider?.id_cont || "").trim();
      const tipServiciu = this._normalizeText(provider?.tip_serviciu || provider?.tip_utilitate || "");
      const wantsGas = tipServiciu.includes("gaz");
      const wantsElectric = tipServiciu.includes("electric") || tipServiciu.includes("energie");
      const isOtherProviderEntity = (stateObj) => {
        const text = this._entityFriendlyText(stateObj);
        const entityId = String(stateObj?.entity_id || "").toLowerCase();
        return entityId.includes("hidro") || entityId.includes("hidroelectrica") || entityId.includes("myelectrica") || entityId.includes("apa_canal") || entityId.includes("apa_brasov") || entityId.includes("apa_oradea") || entityId.includes("apa_galati") || entityId.includes("hidro_prahova") || entityId.includes("apacanal") || entityId.includes("ebloc") || text.includes("hidro") || text.includes("hidroelectrica") || text.includes("myelectrica") || text.includes("apa canal") || text.includes("apa brasov") || text.includes("apă brașov") || text.includes("apa oradea") || text.includes("apă oradea") || text.includes("apa galati") || text.includes("apă galați") || text.includes("apa canal galati") || text.includes("hidro prahova") || text.includes("ebloc");
      };
      const scoreEonEntity = (stateObj, kind) => {
        if (!stateObj?.entity_id?.startsWith(`${kind}.`)) return -1;
        if (isOtherProviderEntity(stateObj)) return -1;
        const entityId = String(stateObj.entity_id || "").toLowerCase();
        const text = this._entityFriendlyText(stateObj);
        const attrs = stateObj.attributes || {};
        let score = 0;
        if (entityId.includes("eon")) score += 160;
        if (text.includes("eon")) score += 80;
        if (idCont && entityId.includes(idCont)) score += 160;
        if (idCont && String(attrs.id_cont ?? "").trim() === idCont) score += 180;
        if (entityId.includes(base)) score += 120;
        if (this._textMatchesAny(text, terms)) score += 90;
        if (this._textMatchesAny(entityId, terms)) score += 50;
        if (kind === "number") {
          if (text.includes("index gaz") || entityId.includes("index_gaz")) score += wantsGas ? 120 : 40;
          if (text.includes("index energie") || entityId.includes("index_energie")) score += wantsElectric ? 120 : 40;
          if (text.includes("index") || entityId.includes("index")) score += 40;
        }
        if (kind === "button") {
          if (!text.includes("trimite index") && !entityId.includes("trimite_index")) return -1;
          if (text.includes("gaz") || entityId.includes("gaz")) score += wantsGas ? 120 : 30;
          if (text.includes("energie") || text.includes("electric") || entityId.includes("energie") || entityId.includes("electric")) score += wantsElectric ? 120 : 30;
          if (entityId.includes("eon")) score += 160;
        }
        if (kind === "sensor") {
          if (text.includes("index gaz") || entityId.includes("index_gaz")) score += wantsGas ? 90 : 30;
          if (text.includes("index energie") || entityId.includes("index_energie")) score += wantsElectric ? 90 : 30;
          if (text.includes("index") || entityId.includes("index")) score += 30;
        }
        return score;
      };
      const bestEntity = (kind, minimumScore) => {
        let best = null;
        let bestScore = -1;
        for (const stateObj of Object.values(states)) {
          const score = scoreEonEntity(stateObj, kind);
          if (score > bestScore) { best = stateObj; bestScore = score; }
        }
        return bestScore >= minimumScore ? best : null;
      };
      const exactNumber = states[`number.${base}_index`] || states[`number.${base}_index_gaz`] || states[`number.${base}_index_energie_electrica`] || null;
      const numberEntity = exactNumber && !isOtherProviderEntity(exactNumber) ? exactNumber : bestEntity("number", 120);
      let currentEntity = states[`sensor.${base}_index_contor`] || states[`sensor.${base}_index_energie_electrica`] || states[`sensor.${base}_index_gaz`] || null;
      if (currentEntity && isOtherProviderEntity(currentEntity)) currentEntity = null;
      if (!currentEntity) currentEntity = bestEntity("sensor", 120);
      const exactButton = states[`button.${base}_trimite_index`] || states[`button.${base}_trimite_index_gaz`] || states[`button.${base}_trimite_index_energie_electrica`] || null;
      const buttonEntity = exactButton && !isOtherProviderEntity(exactButton) ? exactButton : bestEntity("button", 120);
      if (numberEntity && buttonEntity) controls.push({ key: `${providerKey}_${provider.id_cont || base}`, providerKey, label: "Index de transmis", numberEntityId: numberEntity.entity_id, buttonEntityId: buttonEntity.entity_id, currentEntityId: currentEntity?.entity_id || null });
      return controls;
    }

    if (providerKey === "myelectrica") {
      const parts = sensorObject.split("_");
      const slug = parts.slice(3, -1).join("_");
      const numberEntityId = `number.utilitati_romania_myelectrica_${slug}_index_contor`;
      const numberEntity = states[numberEntityId] || null;
      let currentEntity = Object.values(states).find((stateObj) => stateObj.entity_id.startsWith("sensor.") && String(stateObj.attributes?.id_cont ?? "") === String(provider.id_cont ?? "") && (stateObj.entity_id.includes("index_contor") || this._entityFriendlyText(stateObj).includes("index contor")));
      if (!currentEntity) currentEntity = Object.values(states).find((stateObj) => stateObj.entity_id.startsWith("sensor.") && this._textMatchesAny(this._entityFriendlyText(stateObj), terms) && (this._entityFriendlyText(stateObj).includes("index contor") || stateObj.entity_id.includes("index")));
      const buttonEntity = Object.values(states).find((stateObj) => stateObj.entity_id.startsWith("button.") && this._entityFriendlyText(stateObj).includes("trimite index") && this._textMatchesAny(this._entityFriendlyText(stateObj), terms));
      if (numberEntity && buttonEntity) controls.push({ key: `${providerKey}_${provider.id_cont || slug}`, label: "Index de transmis", numberEntityId, buttonEntityId: buttonEntity.entity_id, currentEntityId: currentEntity?.entity_id || null });
      return controls;
    }

    if (providerKey === "engie" || providerKey === "apa_canal" || providerKey === "apa_brasov" || providerKey === "apa_oradea" || providerKey === "apa_galati" || providerKey === "hidro_prahova") {
      const base = sensorObject.replace(/_citire_index_permisa$/, "").replace(/_citire_permisa$/, "");
      const attrs = readingSensor.attributes || {};
      const sensorIdCont = String(attrs.id_cont ?? provider?.id_cont ?? "").trim();
      const sensorIdContract = String(attrs.id_contract ?? provider?.id_contract ?? "").trim();
      const expectedNumberEntityId = `number.${base}_index_de_transmis`;
      const expectedButtonEntityId = `button.${base}_trimite_index`;
      const currentEntityId = `sensor.${base}_ultimul_index`;
      const matchesApaCanalContext = (stateObj) => {
        const stateAttrs = stateObj?.attributes || {};
        const idCont = String(stateAttrs.id_cont ?? "").trim();
        const idContract = String(stateAttrs.id_contract ?? "").trim();
        if (sensorIdCont && idCont && idCont === sensorIdCont) return true;
        if (sensorIdContract && idContract && idContract === sensorIdContract) return true;
        const text = this._entityFriendlyText(stateObj);
        return this._textMatchesAny(text, terms) || stateObj.entity_id.includes(base);
      };
      const numberEntity = (attrs.number_entity_id && states[attrs.number_entity_id]) || states[expectedNumberEntityId] || Object.values(states).find((stateObj) => stateObj.entity_id.startsWith("number.") && this._entityFriendlyText(stateObj).includes("index de transmis") && matchesApaCanalContext(stateObj));
      const buttonEntity = (attrs.button_entity_id && states[attrs.button_entity_id]) || states[expectedButtonEntityId] || Object.values(states).find((stateObj) => stateObj.entity_id.startsWith("button.") && this._entityFriendlyText(stateObj).includes("trimite index") && matchesApaCanalContext(stateObj));
      const currentEntity = states[currentEntityId] || states[`sensor.${base}_index_contor`] || Object.values(states).find((stateObj) => {
        if (!stateObj.entity_id.startsWith("sensor.")) return false;
        const text = this._entityFriendlyText(stateObj);
        const stateAttrs = stateObj.attributes || {};
        const idCont = String(stateAttrs.id_cont ?? "").trim();
        const idContract = String(stateAttrs.id_contract ?? "").trim();
        const sameContext = (sensorIdCont && idCont && idCont === sensorIdCont) || (sensorIdContract && idContract && idContract === sensorIdContract);
        return (text.includes("ultimul index") || text.includes("index contor") || stateObj.entity_id.includes("index_contor")) && (sameContext || this._textMatchesAny(text, terms) || stateObj.entity_id.includes(base));
      });
      if (numberEntity && buttonEntity) {
        controls.push({ key: `${providerKey}_${provider.id_cont || sensorIdCont || base}`, label: "Index de transmis", numberEntityId: numberEntity.entity_id, buttonEntityId: buttonEntity.entity_id, currentEntityId: currentEntity?.entity_id || null });
      } else if (currentEntity) {
        controls.push({ key: `${providerKey}_${provider.id_cont || sensorIdCont || base}_current`, label: "Index curent", numberEntityId: null, buttonEntityId: null, currentEntityId: currentEntity.entity_id });
      }
      return controls;
    }
    return [];
  }

  _getReadingData(location, provider) {
    const cacheKey = this._makeKey(location.locatie_cheie, provider.furnizor, provider.id_cont, provider.id_contract);
    if (this._readingCache.has(cacheKey)) return this._readingCache.get(cacheKey);
    const providerKey = this._providerKey(provider);

    if (providerKey === "ebloc" && provider?.reading_available) {
      const isOpen = provider.reading_is_open === true || this._normalizeText(provider.reading_is_open) === "da";
      const days = Number(provider.reading_days_until);
      const result = { available: true, isOpen, controls: [], start: null, end: null, period: provider.reading_period || null, daysUntil: Number.isFinite(days) ? days : null, badge: isOpen ? "Citire deschisă" : Number.isFinite(days) && days > 0 ? `Citire în ${days} zile` : null };
      this._readingCache.set(cacheKey, result);
      return result;
    }

    const readingSensor = this._findReadingSensor(location, provider);
    if (!readingSensor) {
      const empty = { available: false, isOpen: false, controls: [], start: null, end: null, period: null, badge: null };
      this._readingCache.set(cacheKey, empty);
      return empty;
    }
    const windowInfo = this._extractWindowInfo(readingSensor);
    const controls = this._deriveControlsFromReadingSensor(location, provider, readingSensor).map((control) => {
      const numberState = control.numberEntityId ? this._hass.states[control.numberEntityId] : null;
      const currentState = control.currentEntityId ? this._hass.states[control.currentEntityId] : null;
      return { ...control, numberState, currentState, unit: numberState?.attributes?.unit_of_measurement || currentState?.attributes?.unit_of_measurement || "", currentValue: currentState ? currentState.state : null };
    });
    const result = { available: true, isOpen: !!windowInfo.isOpen, start: windowInfo.start, end: windowInfo.end, period: windowInfo.start && windowInfo.end ? `${this._date(windowInfo.start)} - ${this._date(windowInfo.end)}` : null, badge: windowInfo.isOpen ? "Citire deschisă" : "În afara perioadei", readingSensorEntityId: readingSensor.entity_id, controls };
    this._readingCache.set(cacheKey, result);
    return result;
  }

  _readingPeriodLabel(data) {
    if (!data?.available) return "Nu există entități de citire detectate";
    if (data.period) return data.period;
    if (data.start && data.end) return `${this._date(data.start)} - ${this._date(data.end)}`;
    return data.isOpen ? "Perioada de transmitere este activă" : "În afara perioadei de transmitere";
  }

  _readingSortValue(item) {
    const data = this._getReadingData(item.location, item.provider);
    const start = this._parseDateLike(data.start) || this._parseDateLike(String(data.period || "").split("-")[0]);
    const days = Number(data.daysUntil);
    let group = 3;
    if (data.isOpen) group = 0;
    else if (Number.isFinite(days) && days >= 0) group = 1;
    else if (start) group = 1;
    else if (data.available) group = 2;
    return { group, time: start ? start.getTime() : Number.POSITIVE_INFINITY, name: `${this._providerName(item.provider)} ${this._displayLocationName(item.location) || ""}` };
  }

  _readingEntryKey(location, provider, data) {
    const providerKey = this._providerKey(provider);
    const controls = Array.isArray(data?.controls) ? data.controls : [];
    const entityParts = [data?.readingSensorEntityId, ...controls.flatMap((control) => [control?.numberEntityId, control?.buttonEntityId, control?.currentEntityId])]
      .filter(Boolean)
      .map((value) => String(value).trim().toLowerCase())
      .sort();
    if (entityParts.length) return `${providerKey}|entities|${entityParts.join("|")}`;

    const technicalParts = [
      provider?.entry_id,
      provider?.config_entry_id,
      provider?.id_cont,
      provider?.id_contract,
      provider?.cod_client,
      provider?.pod,
      provider?.ppe,
      location?.locatie_cheie,
    ].filter((value) => value !== undefined && value !== null && String(value).trim() !== "").map((value) => String(value).trim().toLowerCase());
    if (technicalParts.length) return `${providerKey}|technical|${technicalParts.join("|")}`;

    return `${providerKey}|display|${this._normalizeText(`${this._displayLocationName(location)} ${provider?.locatie || provider?.adresa || provider?.adresa_originala || ""}`)}`;
  }

  _readingEntryScore(entry) {
    const data = entry?.data || {};
    const provider = entry?.provider || {};
    const controls = Array.isArray(data.controls) ? data.controls : [];
    const entityIds = [data.readingSensorEntityId, ...controls.flatMap((control) => [control?.numberEntityId, control?.buttonEntityId, control?.currentEntityId])].filter(Boolean);
    let score = 0;
    if (data.available) score += 1000;
    if (data.isOpen) score += 400;
    if (data.readingSensorEntityId) score += 250;
    if (data.start || data.end || data.period) score += 120;
    score += Math.min(controls.length, 4) * 100;
    if (controls.some((control) => control?.numberEntityId && control?.buttonEntityId)) score += 300;
    if (controls.some((control) => control?.currentEntityId)) score += 120;
    if (provider.entry_id || provider.config_entry_id) score += 80;
    if (provider.id_cont) score += 60;
    if (provider.id_contract) score += 60;
    if (provider.cod_client || provider.pod || provider.ppe) score += 40;
    const newestGeneration = entityIds.reduce((max, entityId) => Math.max(max, this._readingEntityGeneration(entityId)), 1);
    score += newestGeneration;
    return score;
  }

  _readingEntityGeneration(entityId) {
    const text = String(entityId || "");
    const match = text.match(/_(\d+)$/);
    return match ? Number(match[1]) : 1;
  }

  _shouldShowReadingEntry(data) {
    if (!data?.available) return false;
    if (data.readingSensorEntityId) return true;
    const controls = Array.isArray(data.controls) ? data.controls : [];
    if (controls.some((control) => control?.numberEntityId || control?.buttonEntityId || control?.currentEntityId)) return true;
    if (data.period || data.start || data.end || Number.isFinite(Number(data.daysUntil))) return true;
    return false;
  }

  _collectReadingEntries(locations) {
    const entriesByKey = new Map();
    for (const item of this._allProviders(locations)) {
      const data = this._getReadingData(item.location, item.provider);
      if (!this._shouldShowReadingEntry(data)) continue;
      const entry = { ...item, data };
      const key = this._readingEntryKey(item.location, item.provider, data);
      const existing = entriesByKey.get(key);
      if (!existing || this._readingEntryScore(entry) >= this._readingEntryScore(existing)) {
        entriesByKey.set(key, entry);
      }
    }
    return Array.from(entriesByKey.values()).sort((a, b) => {
      const aa = this._readingSortValue(a);
      const bb = this._readingSortValue(b);
      if (aa.group !== bb.group) return aa.group - bb.group;
      if (aa.time !== bb.time) return aa.time - bb.time;
      return aa.name.localeCompare(bb.name, "ro");
    });
  }

  _renderReadings(locations) {
    const providers = this._collectReadingEntries(locations);
    return `
      <section class="panel-card">
        <div class="card-head"><div><span class="eyebrow">indexuri</span><h2>Perioade de transmitere</h2></div></div>
        <div class="reading-list">
          ${providers.length ? providers.map(({ location, provider, data }) => this._readingRow(location, provider, data)).join("") : `<div class="empty">Nu există furnizori disponibili pentru indexuri.</div>`}
        </div>
      </section>
    `;
  }

  _readingRow(location, provider, data = null) {
    data = data || this._getReadingData(location, provider);
    const controls = data.controls || [];
    const current = controls.map((control) => control.currentValue !== null && control.currentValue !== undefined ? `${control.currentValue}${control.unit ? ` ${control.unit}` : ""}` : null).filter(Boolean).join(" / ") || "—";
    const tone = data.isOpen ? "open" : data.available ? "closed" : "missing";
    const submitControls = data.isOpen && controls.length ? `<div class="reading-controls">${controls.map((control) => this._renderReadingControl(location, provider, control)).join("")}</div>` : "";
    return `
      <article class="reading-row ${tone}">
        <div class="provider-badge">${this._escape(this._providerName(provider).slice(0, 2).toUpperCase())}</div>
        <div class="reading-main"><strong>${this._escape(this._providerName(provider))}</strong><span>${this._escape(this._displayLocationName(location))}</span></div>
        <div class="reading-period"><span>Perioadă</span><strong>${this._escape(this._readingPeriodLabel(data))}</strong></div>
        <div class="reading-current"><span>Index curent</span><strong>${this._escape(current)}</strong></div>
        <span class="pill ${tone}">${this._escape(data.badge || (data.available ? "Închisă" : "Nedetectat"))}</span>
        ${submitControls}
      </article>
    `;
  }

  _renderReadingControl(location, provider, control) {
    const key = control.buttonEntityId || control.numberEntityId || control.key;
    const action = this._actions.get(`reading__${key}`);
    const draft = this._readingDrafts.get(key) || "";
    const unit = control.unit || "";
    return `
      <div class="reading-control" data-reading-control data-provider="${this._escape(this._providerKey(provider))}" data-entry-id="${this._escape(provider?.entry_id || provider?.config_entry_id || "")}" data-id-cont="${this._escape(provider?.id_cont || "")}" data-id-contract="${this._escape(provider?.id_contract || "")}" data-number-entity="${this._escape(control.numberEntityId || "")}" data-button-entity="${this._escape(control.buttonEntityId || "")}" data-current-entity="${this._escape(control.currentEntityId || "")}" data-current-value="${this._escape(control.currentValue ?? "")}" data-unit="${this._escape(unit)}">
        <label>${this._escape(control.label || "Index de transmis")}</label>
        <input class="reading-input" type="number" inputmode="decimal" step="any" placeholder="Index nou${unit ? ` (${unit})` : ""}" value="${this._escape(draft)}">
        <button class="primary dark reading-submit" data-reading-submit ${action?.status === "busy" ? "disabled" : ""}>${action?.status === "busy" ? "Se trimite..." : "Trimite index"}</button>
        ${action?.message ? `<div class="action-message ${action.status === "error" ? "error" : "ok"}">${this._escape(action.message)}</div>` : ""}
      </div>
    `;
  }

  _findEntityByEntityId(entityId) {
    const stateObj = this._hass?.states?.[entityId];
    if (!stateObj) return null;
    const state = String(stateObj.state ?? "").trim().toLowerCase();
    if (["unknown", "unavailable"].includes(state)) return null;
    return stateObj;
  }

  _findEntityByFriendlyName(domain, friendlyNames) {
    const wanted = (friendlyNames || []).map((name) => this._normalizeText(name)).filter(Boolean);
    if (!wanted.length || !this._hass?.states) return null;
    for (const stateObj of Object.values(this._hass.states)) {
      if (!stateObj?.entity_id?.startsWith(`${domain}.`)) continue;
      const friendly = this._normalizeText(stateObj?.attributes?.friendly_name || "");
      if (!friendly) continue;
      const belongsToUtilities = stateObj.entity_id.includes("utilitati_romania") || stateObj.entity_id.includes("administrare_integrare") || friendly.includes("utilitati") || friendly.includes("facturi");
      if (!belongsToUtilities) continue;
      if (wanted.some((name) => friendly.includes(name))) return stateObj;
    }
    return null;
  }

  _resolveEntity(domain, entityIds, friendlyNames) {
    for (const entityId of entityIds || []) {
      const stateObj = this._findEntityByEntityId(entityId);
      if (stateObj) return stateObj;
    }
    return this._findEntityByFriendlyName(domain, friendlyNames);
  }

  _licenseStates() {
    const statusEntity = this._resolveEntity("sensor", ["sensor.utilitati_romania_status_licenta", "sensor.administrare_integrare_status_licenta", "sensor.status_licenta"], ["status licenta", "status licență"]);
    const planEntity = this._resolveEntity("sensor", ["sensor.utilitati_romania_plan_licenta", "sensor.administrare_integrare_plan_licenta", "sensor.plan_licenta"], ["plan licenta", "plan licență"]);
    const checkedEntity = this._resolveEntity("sensor", ["sensor.utilitati_romania_ultima_verificare_licenta", "sensor.administrare_integrare_ultima_verificare_licenta", "sensor.ultima_verificare_licenta"], ["ultima verificare licenta", "ultima verificare licență"]);
    const accountEntity = this._resolveEntity("sensor", ["sensor.utilitati_romania_cont_licenta", "sensor.administrare_integrare_cont_licenta", "sensor.cont_licenta", "sensor.utilitati_romania_utilizator_licenta"], ["cont licenta", "cont licență"]);
    const messageEntity = this._resolveEntity("sensor", ["sensor.utilitati_romania_mesaj_licenta", "sensor.administrare_integrare_mesaj_licenta", "sensor.mesaj_licenta"], ["mesaj licenta", "mesaj licență"]);
    const keyEntity = this._resolveEntity("sensor", ["sensor.utilitati_romania_cod_licenta_mascat", "sensor.administrare_integrare_cod_licenta_mascat", "sensor.cod_licenta_mascat"], ["cod licenta mascat", "cod licență mascat", "cheie licenta mascata", "cheie licență mascată"]);
    return {
      status: statusEntity?.state || "necunoscut",
      plan: planEntity?.state || "—",
      account: accountEntity?.state || "—",
      checked: checkedEntity?.state || "—",
      key: keyEntity?.state || "—",
      message: messageEntity?.state || "—",
    };
  }

  _licenseEntities() {
    const textEntity = this._resolveEntity("text", ["text.utilitati_romania_cod_licenta_noua", "text.administrare_integrare_cod_licenta_noua", "text.cod_licenta_noua"], ["cod licenta nou", "licenta noua", "cod licență nou", "licență nouă", "cod licenta", "cod licență"]);
    const buttonEntity = this._resolveEntity("button", ["button.utilitati_romania_aplica_licenta", "button.administrare_integrare_aplica_licenta", "button.aplica_licenta"], ["aplica licenta", "aplică licență"]);
    return {
      text: textEntity?.entity_id || "text.utilitati_romania_cod_licenta_noua",
      button: buttonEntity?.entity_id || "button.utilitati_romania_aplica_licenta",
      currentCode: textEntity?.state && !["unknown", "unavailable"].includes(String(textEntity.state).toLowerCase()) ? textEntity.state : "",
    };
  }

  _adminReloadEntity() {
    return this._resolveEntity("button", [
      "button.utilitati_romania_reload_all_subs",
      "button.administrare_integrare_reload_all_subs",
      "button.reload_all_subs",
    ], [
      "reload all subs",
      "reincarca toate subintegrarile",
      "reîncarcă toate subintegrările",
    ]);
  }

  _adminVerifyLicenseEntity() {
    return this._resolveEntity("button", [
      "button.utilitati_romania_verifica_licenta",
      "button.administrare_integrare_verifica_licenta",
      "button.verifica_licenta",
    ], [
      "verifica licenta",
      "verifică licență",
      "verifica licența",
      "verifică licența",
    ]);
  }

  _renderLicense() {
    const lic = this._licenseStates();
    const entities = this._licenseEntities();
    const licenseValue = this._licenseDraft || entities.currentCode || "";
    const licenseStatus = String(lic.status || "").toLowerCase();
    const licensePlan = String(lic.plan || "").toLowerCase();
    const isTrial = licenseStatus.includes("trial") || licensePlan.includes("trial");
    const isFullLicense = licenseStatus.includes("active") && !isTrial;
    const active = isFullLicense || isTrial;
    const supportTitle = isFullLicense ? "Susține în continuare dezvoltarea proiectului" : "Susține dezvoltarea proiectului";
    const supportText = isFullLicense
      ? "Ai deja o licență activă. Dacă integrarea îți este utilă, poți susține în continuare dezvoltarea, mentenanța și adaptarea proiectului atunci când furnizorii schimbă portalurile, aplicațiile sau API-urile folosite."
      : "Licența ajută la susținerea dezvoltării, mentenanței și adaptării integrării atunci când furnizorii schimbă portalurile, aplicațiile sau API-urile folosite.";
    const supportLicenseText = isFullLicense
      ? "Donațiile suplimentare nu sunt obligatorii, dar ajută la menținerea proiectului activ și la acoperirea timpului de dezvoltare."
      : "Poți obține o licență printr-o donație minimă pe Buy Me a Coffee. După donație, codul de licență poate fi introdus în câmpul de mai sus.";
    const supportButtonText = isFullLicense ? "Susține proiectul prin Buy Me a Coffee" : "Obține licență prin Buy Me a Coffee";
    const supportThanksText = isFullLicense
      ? "Mulțumim pentru susținere și pentru folosirea integrării."
      : "Mulțumim pentru susținere. Fiecare donație ajută la menținerea proiectului activ.";
    const action = this._actions.get("license");
    const reloadAction = this._actions.get("reload_providers");
    const verifyAction = this._actions.get("verify_license");
    const reloadEntity = this._adminReloadEntity();
    const verifyEntity = this._adminVerifyLicenseEntity();
    return `
      <section class="panel-card license-card ${active ? "ok" : "warn"}">
        <div class="license-shield"><ha-icon icon="mdi:shield-check"></ha-icon></div>
        <div><span class="eyebrow">licență</span><h2>${this._escape(lic.status)}</h2><p>${this._escape(lic.message || "Statusul licenței este citit din entitățile de administrare ale integrării.")}</p></div>
      </section>
      <section class="panel-card">
        <div class="details-grid">
          <div><span>Plan</span><strong>${this._escape(lic.plan)}</strong></div>
          <div><span>Cont</span><strong>${this._escape(lic.account)}</strong></div>
          <div><span>Licență activă</span><strong>${this._escape(lic.key || "—")}</strong></div>
          <div><span>Ultima verificare</span><strong>${this._escape(this._date(lic.checked))}</strong></div>
        </div>
      </section>
      <section class="panel-card">
        <div class="card-head"><div><span class="eyebrow">actualizare</span><h2>Introdu licență nouă</h2></div></div>
        <div class="license-form">
          <input id="license-input" type="text" autocomplete="off" placeholder="Cod licență" value="${this._escape(licenseValue)}">
          <button class="primary dark" data-apply-license ${action?.status === "busy" ? "disabled" : ""}>${action?.status === "busy" ? "Se verifică..." : "Aplică licența"}</button>
        </div>
        <p class="license-hint">Câmpul poate afișa ultimul cod introdus pentru validare. Licența activă curentă este afișată mascat în secțiunea de mai sus.</p>
        <div class="license-reload-box">
          <div>
            <h3>Verificare licență</h3>
            <p>Verifică manual licența curentă salvată în integrare. Este util după modificări în portalul de licențiere sau dacă vrei să confirmi rapid statusul.</p>
          </div>
          <button class="ghost strong" data-verify-license ${verifyAction?.status === "busy" || !verifyEntity ? "disabled" : ""}>
            <ha-icon icon="mdi:shield-sync"></ha-icon>
            <span>${verifyAction?.status === "busy" ? "Se verifică..." : "Verifică licența"}</span>
          </button>
        </div>
        ${verifyAction?.message ? `<div class="action-message ${verifyAction.status === "error" ? "error" : "ok"}">${this._escape(verifyAction.message)}</div>` : ""}
        <div class="license-reload-box">
          <div>
            <h3>După activarea licenței</h3>
            <p>Dacă perioada trial a expirat și unii furnizori au rămas indisponibili, reîncarcă furnizorii manual. Nu facem acest reload automat, deoarece unele subintegrări pot dura mult.</p>
          </div>
          <button class="ghost strong" data-reload-providers ${reloadAction?.status === "busy" ? "disabled" : ""}>
            <ha-icon icon="mdi:reload-alert"></ha-icon>
            <span>${reloadAction?.status === "busy" ? "Se reîncarcă..." : "Reîncarcă furnizorii"}</span>
          </button>
        </div>
        ${reloadAction?.message ? `<div class="action-message ${reloadAction.status === "error" ? "error" : "ok"}">${this._escape(reloadAction.message)}</div>` : ""}
        <div class="license-support-box">
          <div class="license-support-icon"><ha-icon icon="mdi:heart-outline"></ha-icon></div>
          <div>
            <h3>${this._escape(supportTitle)}</h3>
            <p>Utilități România este un proiect HAForge Labs dezvoltat independent pentru comunitatea Home Assistant din România.</p>
            <p>${this._escape(supportText)}</p>
            <p>${this._escape(supportLicenseText)}</p>
            <div class="license-links">
              <a class="bmc-button" href="https://www.buymeacoffee.com/haforgelabs" target="_blank" rel="noopener noreferrer"><ha-icon icon="mdi:coffee"></ha-icon><span>${this._escape(supportButtonText)}</span></a>
            </div>
            <small>${this._escape(supportThanksText)}</small>
          </div>
        </div>
        ${action?.message ? `<div class="action-message ${action.status === "error" ? "error" : "ok"}">${this._escape(action.message)}</div>` : ""}
      </section>
    `;
  }

  _renderContact() {
    return `
      <section class="panel-card contact-card">
        <div class="card-head"><div><span class="eyebrow">contact</span><h2>HAForge Labs</h2></div></div>
        <p>Pentru suport, sugestii sau raportarea unei probleme legate de integrare, folosește canalele de mai jos.</p>
        <div class="support-note"><ha-icon icon="mdi:information-outline"></ha-icon><span>Pentru suport, menționează versiunea integrării, furnizorul afectat și mesajul din tabul Diagnostic. Nu publica niciodată codul complet de licență, date de autentificare sau coduri client complete.</span></div>
        <div class="contact-actions">
          <a class="contact-action" href="https://haforgelabs.ro" target="_blank" rel="noopener noreferrer"><ha-icon icon="mdi:web"></ha-icon><span>Site HAForge Labs</span></a>
          <a class="contact-action" href="mailto:contact@haforgelabs.ro"><ha-icon icon="mdi:email-outline"></ha-icon><span>contact@haforgelabs.ro</span></a>
          <a class="contact-action" href="https://github.com/HAForgeLabs/utilitati_romania/issues" target="_blank" rel="noopener noreferrer"><ha-icon icon="mdi:github"></ha-icon><span>Raportează pe GitHub</span></a>
        </div>
      </section>
    `;
  }

  _diagnosticPayload(summary) {
    const lic = this._licenseStates();
    const providers = this._allProviders(summary.locations || []).map(({ location, provider }) => {
      const reading = this._getReadingData(location, provider);
      return {
        locatie: this._billingDisplayName(location, provider),
        furnizor: this._providerName(provider),
        status_factura: this._statusLabel(this._status(provider)),
        scadenta: this._date(this._providerDue(provider)),
        valoare: this._money(this._providerAmount(provider), provider?.currency || "RON"),
        citire: reading.isOpen ? "deschisă" : reading.available ? "închisă" : "nedetectată",
      };
    });
    return {
      integrare: "Utilități România",
      senzor_agregat: summary.entityId || "nedetectat",
      stare_senzor: summary.state?.state || "indisponibil",
      ultima_eroare: summary.attrs.ultima_eroare || "fără erori",
      locatii: summary.locations?.length || 0,
      facturi: summary.attrs.numar_facturi ?? providers.length,
      licenta: this._safeDiagnosticLicense(lic),
      furnizori: providers,
    };
  }


  _billingGroupEntities(summary = this._summary()) {
    const states = this._hass?.states || {};
    const providerDefinitions = [
      { slug: "apa canal sibiu", label: "Apă Canal Sibiu" },
      { slug: "apa brasov", label: "Apă Brașov" },
      { slug: "digi romania", label: "Digi România" },
      { slug: "distributie energie electrica romania", label: "Distribuție Energie Electrică România" },
      { slug: "e on romania", label: "E.ON România" },
      { slug: "eon romania", label: "E.ON România" },
      { slug: "hidroelectrica", label: "Hidroelectrica" },
      { slug: "e bloc ro", label: "e-bloc.ro" },
      { slug: "orange", label: "Orange" },
      { slug: "nova", label: "Nova" },
      { slug: "myelectrica", label: "myElectrica" },
      { slug: "deer", label: "DEER" },
    ];
    const providerAliases = new Map(providerDefinitions.map((item) => [this._normalizeText(item.slug), item.label]));
    const compact = (value) => this._normalizeText(value).replace(/\s+/g, "");
    const cleanGroupWords = (value) => String(value || "")
      .replace(/_/g, " ")
      .replace(/\s+/g, " ")
      .replace(/^grupare\s+facturi\s*/iu, "")
      .replace(/\s*[·|]\s*grupare\s+facturi\s*/giu, " - ")
      .replace(/\s*-\s*grupare\s+facturi\s+.+$/iu, "")
      .replace(/\s+grupare\s+facturi\s+.+$/iu, "")
      .replace(/\s+grupare\s+facturi\s*$/iu, "")
      .trim();
    const cleanDisplay = (value) => cleanGroupWords(value)
      .replace(/\bCf\b/gi, "")
      .replace(/\bCont\b/gi, "")
      .replace(/\s+/g, " ")
      .replace(/\s+,/g, ",")
      .trim();
    const titleFromEntity = (entityId) => entityId
      .replace(/^text\./, "")
      .replace(/^grupare_facturi_/, "")
      .replace(/_grupare_facturi_/g, " - ")
      .replace(/_/g, " ")
      .replace(/\b\w/g, (letter) => letter.toUpperCase());
    const providerLabelFromText = (text) => {
      const normalized = this._normalizeText(text);
      const compactText = compact(text);
      let best = null;
      for (const [slug, label] of providerAliases.entries()) {
        const compactSlug = compact(slug);
        if (normalized.includes(slug) || compactText.includes(compactSlug)) {
          if (!best || compactSlug.length > compact(best.slug).length) best = { slug, label };
        }
      }
      return best;
    };
    const entitySourceText = (entityId, friendly) => cleanGroupWords(friendly || titleFromEntity(entityId));
    const buildEntityTerms = (entityId, friendly) => {
      const source = `${entityId} ${friendly || ""}`;
      return this._normalizeText(source)
        .replace(/\btext\b/g, " ")
        .replace(/\bgrupare\b/g, " ")
        .replace(/\bfacturi\b/g, " ")
        .split(/\s+/)
        .filter((term) => term && term.length > 1 && !["ro", "romania", "grupare", "facturi"].includes(term));
    };
    const providerEntries = this._allProviders(summary?.locations || []).map(({ location, provider }) => {
      const name = this._providerName(provider);
      const providerLabel = providerLabelFromText(name)?.label || name;
      const address = cleanDisplay(provider?.adresa_originala || provider?.adresa || provider?.address || "");
      const accountName = cleanDisplay(provider?.nume_cont || provider?.account_name || provider?.cont || "");
      const locationName = cleanDisplay(this._rawLocationName(location));
      const identifiers = [
        provider?.apartament,
        provider?.apartment,
        provider?.id_apartament,
        provider?.id_cont,
        provider?.id_contract,
        provider?.cod_client,
        provider?.pod,
        provider?.ppe,
      ].map((value) => cleanDisplay(value)).filter(Boolean);
      const detail = address || accountName || locationName;
      const label = `${providerLabel}${detail ? ` - ${detail}` : ""}`;
      const haystack = this._normalizeText([name, providerLabel, address, accountName, locationName, location?.locatie_cheie, location?.eticheta_locatie, ...identifiers].join(" "));
      return { providerLabel, label, haystack, detail, identifiers };
    });
    const bestProviderEntry = (entityId, friendly) => {
      const source = entitySourceText(entityId, friendly);
      const provider = providerLabelFromText(source) || providerLabelFromText(entityId) || providerLabelFromText(friendly);
      const terms = buildEntityTerms(entityId, friendly);
      let candidates = providerEntries;
      if (provider?.label) {
        const providerNorm = this._normalizeText(provider.label);
        candidates = candidates.filter((entry) => this._normalizeText(entry.providerLabel) === providerNorm || entry.haystack.includes(providerNorm));
      }
      if (!candidates.length) return null;
      let best = null;
      for (const entry of candidates) {
        let score = 0;
        for (const term of terms) {
          if (entry.haystack.includes(term)) score += Math.min(8, term.length);
        }
        if (provider?.label && this._normalizeText(entry.providerLabel) === this._normalizeText(provider.label)) score += 30;
        if (entry.detail) score += 5;
        if (entry.identifiers?.length) score += 2;
        if (!best || score > best.score) best = { entry, score };
      }
      return best?.score > 0 ? best.entry : null;
    };
    const fallbackLabel = (entityId, friendly) => {
      const source = entitySourceText(entityId, friendly);
      const provider = providerLabelFromText(source) || providerLabelFromText(entityId) || { label: cleanDisplay(source).split(" - ")[0] || "Furnizor" };
      let rest = cleanDisplay(source)
        .replace(new RegExp(`^${provider.label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*-?\\s*`, "i"), "")
        .replace(/^grupare\s+facturi\s*/i, "")
        .trim();
      rest = rest
        .replace(/\s*-\s*$/g, "")
        .replace(/\s+Grupare\s+Facturi\s+.+$/i, "")
        .trim();
      return `${provider.label}${rest ? ` - ${rest}` : ""}`;
    };
    const isGenericEblocGroup = (item) => {
      const label = String(item.label || "").toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
      const entityId = String(item.entityId || "").toLowerCase();
      return entityId.includes("e_bloc_ro") && (
        /_asociatia$/.test(entityId) ||
        /^e-bloc\.ro\s*-\s*asociatia$/.test(label) ||
        /^e-bloc\.ro\s*-\s*asociatie$/.test(label)
      );
    };

    return Object.entries(states)
      .filter(([entityId, state]) => {
        if (!entityId.startsWith("text.")) return false;
        if (entityId.includes("cod_licenta")) return false;

        // Entitățile vechi de grupare pot rămâne în registry după ștergerea
        // sau refacerea unor furnizori. Home Assistant le expune ca
        // unavailable, iar panoul nu trebuie să le mai afișeze în setări.
        if (["unavailable", "unknown"].includes(String(state?.state || "").toLowerCase())) return false;

        const name = String(state?.attributes?.friendly_name || state?.attributes?.name || entityId).toLowerCase();
        return name.includes("grupare facturi") || entityId.includes("grupare_facturi");
      })
      .map(([entityId, state]) => {
        const friendly = String(state?.attributes?.friendly_name || state?.attributes?.name || entityId);
        const explicitLabel = cleanDisplay(state?.attributes?.grupare_facturi_label || "");
        const explicitLocationName = cleanDisplay(state?.attributes?.nume_loc_consum || "");
        const explicitProvider = cleanDisplay(state?.attributes?.furnizor || "");
        const matched = bestProviderEntry(entityId, friendly);
        const friendlyLabel = fallbackLabel(entityId, friendly);
        // Preferăm atributele expuse explicit de entitatea text. Friendly name-ul
        // poate rămâne cel vechi în entity registry după update, iar datele
        // agregate din facturi pot conține aceeași adresă pentru două locații
        // diferite. Atributele se actualizează odată cu entitatea și nu schimbă
        // entity_id / unique_id.
        const label = explicitLabel || (explicitProvider && explicitLocationName ? `${explicitProvider} - ${explicitLocationName}` : "") || matched?.label || friendlyLabel || fallbackLabel(entityId, friendly);
        const savedValue = state?.state && !["unknown", "unavailable"].includes(state.state) ? state.state : "";
        return {
          entityId,
          state,
          friendly,
          provider: label,
          label,
          matchKey: matched ? this._normalizeText(matched.label || label) : this._normalizeText(label),
          context: `Grupare salvată: ${savedValue || "necompletată"}`,
          savedValue,
        };
      })
      .filter((item) => !isGenericEblocGroup(item))
      // Nu deduplicam dupa eticheta afisata. Pentru furnizori precum Hidroelectrica
      // pot exista doua locuri de consum cu adrese/nume foarte apropiate, iar
      // utilizatorul trebuie sa poata corecta gruparea fiecarui cont separat.
      .sort((a, b) => `${a.label} ${a.entityId}`.localeCompare(`${b.label} ${b.entityId}`, "ro"));
  }


  _billingGroupEntitySignature(location, provider) {
    return this._normalizeText([
      this._providerName(provider),
      provider?.furnizor_label,
      provider?.furnizor,
      provider?.provider,
      provider?.adresa_originala,
      provider?.adresa,
      provider?.address,
      provider?.nume_cont,
      provider?.account_name,
      provider?.cont,
      provider?.id_cont,
      provider?.id_contract,
      provider?.cod_client,
      provider?.pod,
      provider?.ppe,
      location?.locatie_cheie,
      location?.eticheta_locatie,
      location?.name,
      this._rawLocationName(location),
      this._bestLocationLabel(this._locationCandidates(location, provider)),
    ].filter(Boolean).join(" "));
  }

  _matchedBillingGroupEntity(location, provider) {
    if (!provider) return null;
    const groups = this._billingGroupEntities(this._summary()).filter((item) => this._cleanLocationCandidate(item?.savedValue));
    if (!groups.length) return null;

    const providerKey = this._providerKey(provider);
    const providerName = this._normalizeText(this._providerName(provider));
    const signature = this._billingGroupEntitySignature(location, provider);
    const identifiers = [
      provider?.id_cont,
      provider?.id_contract,
      provider?.cod_client,
      provider?.pod,
      provider?.ppe,
      provider?.nume_cont,
      provider?.adresa_originala,
      provider?.adresa,
      this._rawLocationName(location),
      location?.locatie_cheie,
      location?.eticheta_locatie,
    ].map((value) => this._normalizeText(value)).filter((value) => value && value.length > 2);

    let best = null;
    for (const item of groups) {
      const entityText = this._normalizeText([item.entityId, item.friendly, item.label, item.provider, item.matchKey].filter(Boolean).join(" "));
      let score = 0;

      if (providerKey && entityText.includes(providerKey.replace(/_/g, " "))) score += 35;
      if (providerKey && entityText.includes(providerKey)) score += 35;
      if (providerName && entityText.includes(providerName)) score += 35;

      for (const identifier of identifiers) {
        const compactIdentifier = identifier.replace(/\s+/g, "");
        const compactEntity = entityText.replace(/\s+/g, "");
        if (identifier && entityText.includes(identifier)) score += Math.min(70, 20 + identifier.length);
        else if (compactIdentifier && compactEntity.includes(compactIdentifier)) score += Math.min(70, 20 + compactIdentifier.length);
      }

      const terms = signature.split(/\s+/).filter((term) => term.length > 2 && !["romania", "facturi", "grupare", "strada", "numar"].includes(term));
      for (const term of terms) {
        if (entityText.includes(term)) score += Math.min(8, term.length);
      }

      if (!best || score > best.score) best = { item, score };
    }

    return best?.score >= 55 ? best.item : null;
  }

  _renderSettings(summary) {
    const mobileSelect = this._mobileDeviceSelectEntity();
    const mobileOptions = Array.isArray(mobileSelect?.attributes?.options) ? mobileSelect.attributes.options : [];
    const selectedMobile = mobileSelect?.state || "none";
    const notificationMobileSelect = this._mobileNotificationSelectEntity();
    const notificationMobileOptions = Array.isArray(notificationMobileSelect?.attributes?.options) ? notificationMobileSelect.attributes.options : [];
    const selectedNotificationMobile = notificationMobileSelect?.state || "none";
    const prefs = this._notificationPreferences();
    const dashboardPrefs = this._dashboardPreferences();
    const aliases = this._locationAliases();
    const action = this._actions.get("settings");
    const locations = summary.locations || [];
    const consumptionPoints = Array.isArray(summary.consumptionPoints) ? summary.consumptionPoints : [];
    const activeConsumptionPoints = consumptionPoints.filter((item) => !item.ignored);
    const ignoredConsumptionPoints = consumptionPoints.filter((item) => item.ignored);
    const providerInitials = (value) => String(value || "LC")
      .replace(/[^a-zA-Z0-9ăâîșțĂÂÎȘȚ ]/g, " ")
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((word) => word[0])
      .join("")
      .toUpperCase() || "LC";
    const renderConsumptionPoint = (item, ignored) => {
      const title = item.eticheta_locatie || item.nume_cont || item.adresa_originala || item.cheie || "Loc de consum";
      const providerLabel = item.furnizor_label || item.furnizor || "Furnizor";
      const chips = [
        providerLabel,
        item.id_cont ? `ID cont: ${item.id_cont}` : null,
        item.id_contract ? `Contract: ${item.id_contract}` : null,
      ].filter(Boolean);
      const safeKey = this._escape(item.cheie || "");
      return `<article class="consumption-point-card ${ignored ? "ignored" : "active"}">
        <div class="consumption-provider-badge">${this._escape(providerInitials(providerLabel))}</div>
        <div class="consumption-point-main">
          <strong>${this._escape(title)}</strong>
          <div class="consumption-point-chips">${chips.map((chip) => `<span>${this._escape(chip)}</span>`).join("") || `<span>fără identificator tehnic afișabil</span>`}</div>
          <details class="consumption-point-key"><summary>Identificator tehnic</summary><code>${this._escape(item.cheie || "-")}</code></details>
        </div>
        <label class="visibility-switch ${ignored ? "off" : "on"}" title="${ignored ? "Reactivează locul de consum" : "Ignoră locul de consum"}">
          <input type="checkbox" data-consumption-visibility="${safeKey}" data-label="${this._escape(title)}" ${ignored ? "" : "checked"} ${action?.status === "busy" ? "disabled" : ""}>
          <span aria-hidden="true"></span>
          <em>${ignored ? "Ignorat" : "Activ"}</em>
        </label>
      </article>`;
    };
    const billingGroups = this._billingGroupEntities(summary);
    const toggle = (key, label, description) => `
      <label class="setting-toggle">
        <input type="checkbox" data-setting-toggle="${this._escape(key)}" ${prefs[key] ? "checked" : ""}>
        <span><strong>${this._escape(label)}</strong><small>${this._escape(description)}</small></span>
      </label>
    `;
    return `
      <section class="panel-card">
        <div class="card-head"><div><span class="eyebrow">administrare</span><h2>Setări rapide</h2></div></div>
        <div class="support-note"><ha-icon icon="mdi:information-outline"></ha-icon><span>Setările de afișare și denumirile de mai jos modifică doar dashboard-ul integrat. Nu redenumesc entitățile Home Assistant și nu modifică dashboard-urile Lovelace existente.</span></div>
        <div class="settings-grid">
          <div class="setting-block">
            <div><span class="eyebrow">aplicații furnizori</span><h3>Dispozitiv mobil</h3><p>Alege telefonul pe care se deschid aplicațiile furnizorilor din butoanele aflate în facturi.</p></div>
            ${mobileSelect ? `
              <select data-mobile-device-select data-entity-id="${this._escape(mobileSelect.entity_id)}">
                ${mobileOptions.map((option) => `<option value="${this._escape(option)}" ${option === selectedMobile ? "selected" : ""}>${this._escape(this._mobileDeviceLabel(option))}</option>`).join("")}
              </select>
              <small class="setting-hint">Entitate: ${this._escape(mobileSelect.entity_id)}</small>
            ` : `<div class="empty">Nu am găsit entitatea de selectare a dispozitivului mobil. Verifică intrarea „Administrare integrare”.</div>`}
          </div>
          <div class="setting-block">
            <div><span class="eyebrow">afișare</span><h3>Preferințe dashboard</h3><p>Setări locale pentru acest panou. Nu modifică dashboard-urile Lovelace ale utilizatorului.</p></div>
            <label class="setting-toggle">
              <input type="checkbox" data-dashboard-pref="compactInvoicesMobile" ${dashboardPrefs.compactInvoicesMobile ? "checked" : ""}>
              <span><strong>Facturi compacte pe mobil</strong><small>Păstrează lista scurtă și afișează detaliile la apăsarea săgeții.</small></span>
            </label>
            <label class="setting-toggle">
              <input type="checkbox" data-dashboard-pref="backButtonEnabled" ${dashboardPrefs.backButtonEnabled ? "checked" : ""}>
              <span><strong>Buton înapoi în header</strong><small>Afișează un buton de navigare în partea de sus a dashboard-ului.</small></span>
            </label>
            <label class="setting-field">
              <span>Text buton</span>
              <small>Ex. Înapoi, Acasă, Dashboard principal</small>
              <input type="text" data-dashboard-pref-text="backButtonLabel" value="${this._escape(this._dashboardDrafts.has("backButtonLabel") ? this._dashboardDrafts.get("backButtonLabel") : (dashboardPrefs.backButtonLabel || "Înapoi"))}" placeholder="Înapoi">
            </label>
            <label class="setting-field">
              <span>Pagina destinație</span>
              <small>Poți folosi o cale Home Assistant, de exemplu /lovelace, /dashboard-home/0 sau valoarea history pentru revenire în browser.</small>
              <input type="text" data-dashboard-pref-text="backButtonTarget" value="${this._escape(this._dashboardDrafts.has("backButtonTarget") ? this._dashboardDrafts.get("backButtonTarget") : (dashboardPrefs.backButtonTarget || "/lovelace"))}" placeholder="/lovelace">
            </label>
          </div>
        </div>
        ${action?.message ? `<div class="action-message ${action.status === "error" ? "error" : "ok"}">${this._escape(action.message)}</div>` : ""}
      </section>

      <section class="panel-card consumption-visibility-card">
        <div class="card-head"><div><span class="eyebrow">locuri de consum</span><h2>Vizibilitate în dashboard</h2><p>Locurile ignorate nu apar în Prezentare, Facturi, Indexuri, totaluri și notificări. Ele rămân aici pentru reactivare.</p></div></div>
        <div class="consumption-stats">
          <div><span>Active</span><strong>${activeConsumptionPoints.length}</strong></div>
          <div><span>Ignorate</span><strong>${ignoredConsumptionPoints.length}</strong></div>
        </div>
        <div class="consumption-sections">
          <div class="consumption-section">
            <div class="consumption-section-head"><strong>Active</strong><span>${activeConsumptionPoints.length} locuri</span></div>
            <div class="consumption-point-list">
              ${activeConsumptionPoints.length ? activeConsumptionPoints.map((item) => renderConsumptionPoint(item, false)).join("") : `<div class="empty">Nu există locuri de consum active detectate.</div>`}
            </div>
          </div>
          <div class="consumption-section">
            <div class="consumption-section-head"><strong>Ignorate</strong><span>${ignoredConsumptionPoints.length} locuri</span></div>
            <div class="consumption-point-list">
              ${ignoredConsumptionPoints.length ? ignoredConsumptionPoints.map((item) => renderConsumptionPoint(item, true)).join("") : `<div class="empty">Nu există locuri de consum ignorate.</div>`}
            </div>
          </div>
        </div>
      </section>
      <section class="panel-card">
        <div class="card-head"><div><span class="eyebrow">notificări</span><h2>Ce notificări primești</h2></div><button class="primary dark small" data-save-notification-settings ${action?.status === "busy" ? "disabled" : ""}>${action?.status === "busy" ? "Se salvează..." : "Salvează notificările"}</button></div>
        <div class="settings-grid compact">
          <div class="setting-block">
            <div><span class="eyebrow">telefon</span><h3>Dispozitiv pentru notificări</h3><p>Alege telefonul pe care se trimit notificările de facturi și indexuri. Notificările persistente din Home Assistant rămân active.</p></div>
            ${notificationMobileSelect ? `
              <select data-mobile-notification-select data-entity-id="${this._escape(notificationMobileSelect.entity_id)}">
                ${notificationMobileOptions.map((option) => `<option value="${this._escape(option)}" ${option === selectedNotificationMobile ? "selected" : ""}>${this._escape(this._mobileDeviceLabel(option))}</option>`).join("")}
              </select>
              <small class="setting-hint">Entitate: ${this._escape(notificationMobileSelect.entity_id)}</small>
            ` : `<div class="empty">Nu am găsit entitatea pentru selectarea telefonului de notificări. Verifică intrarea „Administrare integrare”.</div>`}
          </div>
        </div>
        <div class="settings-list notification-toggles">
          ${toggle("facturi_noi", "Facturi noi", "Primești notificare când integrarea detectează o factură nouă neplătită.")}
          ${toggle("scadente", "Scadențe apropiate", "Primești notificări înainte de scadență, după pragurile configurate în integrare.")}
          ${toggle("indexuri", "Perioade de transmitere index", "Primești notificare când începe perioada de transmitere index pentru furnizorii suportați.")}
        </div>
      </section>
      <section class="panel-card">
        <div class="card-head"><div><span class="eyebrow">grupare facturi</span><h2>Locuri de consum pentru facturi</h2><p>Modifică gruparea reală folosită de card și de senzorul agregat. Valorile se salvează în entitățile de configurare ale integrării.</p></div><button class="primary dark small" data-save-billing-groups ${action?.status === "busy" ? "disabled" : ""}>${action?.status === "busy" ? "Se salvează..." : "Salvează grupările"}</button></div>
        <div class="location-alias-list">
          ${billingGroups.length ? billingGroups.map((item) => {
            const value = this._settingsDrafts.has(`billing__${item.entityId}`) ? this._settingsDrafts.get(`billing__${item.entityId}`) : (item.savedValue || "");
            return `<label class="location-alias-row billing-group-row"><span><strong>${this._escape(item.label || item.provider)}</strong><small>Grupare salvată: ${this._escape(value || "necompletată")}</small><small>Entitate: ${this._escape(item.entityId)}</small></span><input type="text" data-billing-group="${this._escape(item.entityId)}" placeholder="Ex. Frasinului" value="${this._escape(value)}"></label>`;
          }).join("") : `<div class="empty">Nu am găsit entități de grupare facturi. Acestea apar după încărcarea furnizorilor configurați.</div>`}
        </div>
      </section>
      <section class="panel-card">
        <div class="card-head"><div><span class="eyebrow">locații</span><h2>Denumiri afișate doar în dashboard</h2></div><button class="primary dark small" data-save-location-aliases ${action?.status === "busy" ? "disabled" : ""}>${action?.status === "busy" ? "Se salvează..." : "Salvează denumirile"}</button></div>
        <div class="location-alias-list">
          ${locations.length ? locations.map((location) => {
            const key = this._locationKey(location);
            const raw = this._rawLocationName(location);
            const value = this._settingsDrafts.has(`alias__${key}`) ? this._settingsDrafts.get(`alias__${key}`) : (aliases[key] || "");
            return `<label class="location-alias-row"><span><strong>${this._escape(raw)}</strong><small>Cheie: ${this._escape(key)}</small></span><input type="text" data-location-alias="${this._escape(key)}" placeholder="Nume afișat" value="${this._escape(value)}"></label>`;
          }).join("") : `<div class="empty">Nu există locații în senzorul agregat.</div>`}
        </div>
      </section>
    `;
  }

  _renderDiagnostics(summary) {
    const entities = Object.keys(this._hass?.states || {}).filter((id) => id.includes("utilitati") || id.includes("licenta")).length;
    const action = this._actions.get("copy_diagnostics");
    const providers = this._allProviders(summary.locations || []);
    return `
      <section class="panel-card">
        <div class="card-head"><div><span class="eyebrow">diagnostic</span><h2>Stare integrare</h2></div><button class="primary dark small" data-copy-diagnostics>${action?.status === "busy" ? "Se copiază..." : "Copiază diagnostic"}</button></div>
        <div class="details-grid">
          <div><span>Senzor agregat</span><strong>${this._escape(summary.entityId || "nedetectat")}</strong></div>
          <div><span>Disponibilitate</span><strong>${summary.state ? this._escape(summary.state.state) : "indisponibil"}</strong></div>
          <div><span>Entități relevante</span><strong>${entities}</strong></div>
          <div><span>Ultima eroare</span><strong>${this._escape(summary.attrs.ultima_eroare || "fără erori")}</strong></div>
        </div>
        ${action?.message ? `<div class="action-message ${action.status === "error" ? "error" : "ok"}">${this._escape(action.message)}</div>` : ""}
      </section>
      <section class="panel-card">
        <div class="card-head"><div><span class="eyebrow">furnizori</span><h2>Status rapid</h2></div></div>
        <div class="provider-status-list">
          ${providers.length ? providers.map(({ location, provider }) => {
            const reading = this._getReadingData(location, provider);
            const status = this._status(provider);
            const readingTone = reading.isOpen ? "open" : reading.available ? "closed" : "missing";
            return `<article class="provider-status-row"><div><strong>${this._escape(this._providerName(provider))}</strong><span>${this._escape(this._billingDisplayName(location, provider))}</span></div><span class="pill ${status}">${this._escape(this._statusLabel(status))}</span><span class="pill ${readingTone}">${this._escape(reading.isOpen ? "Citire deschisă" : reading.available ? "Citire închisă" : "Citire nedetectată")}</span></article>`;
          }).join("") : `<div class="empty">Nu există furnizori în senzorul agregat.</div>`}
        </div>
      </section>
    `;
  }

  _renderContent(summary) {
    const attrs = summary.attrs;
    const locations = summary.locations;
    if (this._activeTab === "invoices") return this._renderInvoices(locations);
    if (this._activeTab === "readings") return this._renderReadings(locations);
    if (this._activeTab === "license") return this._renderLicense();
    if (this._activeTab === "contact") return this._renderContact();
    if (this._activeTab === "settings") return this._renderSettings(summary);
    if (this._activeTab === "diagnostics") return this._renderDiagnostics(summary);
    return this._renderOverview(attrs, locations);
  }

  _styles() {
    return `
      :host { display:block; min-height:100vh; background:radial-gradient(circle at -70px -90px,#07111f 0,#10223d 250px,transparent 252px),radial-gradient(circle at 100% 0,rgba(78,161,255,.15),transparent 420px),linear-gradient(180deg,#eef4fb 0%,#f7f9fc 42%,#eef3f8 100%); color:#142033; font-family:var(--paper-font-body1_-_font-family, Roboto, Arial, sans-serif); }
      * { box-sizing:border-box; }
      .wrap { max-width:1280px; margin:0 auto; padding:28px clamp(16px,4vw,42px) 48px; }
      .hero { display:grid; grid-template-columns:minmax(0,1fr) 360px; gap:24px; align-items:stretch; margin-bottom:16px; }
      .hero-content { min-height:245px; padding:34px; border-radius:32px; background:radial-gradient(circle at top right,rgba(58,141,255,.52),transparent 36%),linear-gradient(135deg,#14233a,#213752 64%,#2e5f9e); border:1px solid rgba(255,255,255,.26); box-shadow:0 24px 80px rgba(0,0,0,.18); color:#fff; overflow:hidden; position:relative; }
      .hero-content::after { content:""; position:absolute; width:230px; height:230px; border-radius:50%; background:rgba(255,255,255,.09); right:-80px; bottom:-120px; }
      .brand-row { position:relative; z-index:1; display:flex; align-items:center; gap:18px; padding-right:190px; min-height:96px; }
      .utility-logo { width:86px; height:86px; object-fit:contain; border-radius:24px; background:rgba(255,255,255,.12); padding:10px; border:1px solid rgba(255,255,255,.16); flex:0 0 auto; }
      .brand-meta { display:flex; align-items:center; min-width:0; }
      .forge-lockup { position:absolute; top:30px; right:34px; z-index:2; display:grid; grid-template-columns:34px auto; grid-template-rows:auto auto; column-gap:8px; row-gap:2px; align-items:center; color:#8cc4ff; font-size:11px; text-transform:uppercase; letter-spacing:.13em; font-weight:900; white-space:nowrap; text-decoration:none; }
      .forge-logo { grid-row:1 / span 2; width:34px; height:34px; border-radius:11px; object-fit:cover; box-shadow:0 0 24px rgba(0,210,255,.4); }
      .forge-version { grid-column:2; color:rgba(226,242,255,.72); font-size:9px; line-height:1; letter-spacing:.04em; text-transform:none; font-weight:800; }
      .dashboard-back-button { position:absolute; right:34px; bottom:30px; z-index:3; border:1px solid rgba(255,255,255,.24); border-radius:999px; padding:10px 14px; background:rgba(7,17,31,.34); color:#fff; display:inline-flex; align-items:center; gap:8px; font-weight:900; box-shadow:0 12px 28px rgba(0,0,0,.16); backdrop-filter:blur(12px); }
      .dashboard-back-button ha-icon { width:20px; height:20px; }
      .eyebrow { display:block; text-transform:uppercase; letter-spacing:.13em; font-size:11px; font-weight:800; color:#5fa8ff; margin-bottom:6px; }
      .hero-content .eyebrow { color:#8cc4ff; }
      h1 { font-size:clamp(36px,5.2vw,60px); line-height:.95; margin:0; letter-spacing:-.055em; color:#fff; text-shadow:0 2px 18px rgba(0,0,0,.28); }
      h2 { font-size:22px; margin:0; letter-spacing:-.025em; }
      p { margin:0; line-height:1.55; }
      .hero-content p { position:relative; z-index:1; max-width:760px; color:rgba(255,255,255,.86); font-size:16px; margin-top:20px; }
      button { font:inherit; cursor:pointer; }
      .primary { border:0; border-radius:999px; padding:12px 18px; font-weight:800; background:#4ea1ff; color:#fff; box-shadow:0 12px 30px rgba(78,161,255,.35); }
      .primary.dark { background:#112033; box-shadow:0 12px 30px rgba(17,32,51,.2); }
      .primary:disabled { opacity:.62; cursor:default; }
      .primary.small { padding:9px 13px; font-size:13px; }
      .hero-card { padding:28px; border-radius:32px; background:#fff; color:#142033; display:flex; flex-direction:column; justify-content:center; box-shadow:0 24px 70px rgba(0,0,0,.14); position:relative; overflow:hidden; }
      .hero-card::before { content:""; position:absolute; inset:auto -60px -60px auto; width:180px; height:180px; border-radius:50%; background:rgba(78,161,255,.14); }
      .hero-card.attention::before { background:rgba(255,146,69,.18); }
      .hero-card-label { color:#6b7b90; font-weight:800; text-transform:uppercase; letter-spacing:.1em; font-size:12px; }
      .hero-card strong { font-size:38px; letter-spacing:-.05em; margin:12px 0 6px; }
      .hero-card small { color:#6b7b90; font-weight:700; }
      .tabs { position:sticky; top:0; z-index:5; display:flex; gap:8px; padding:10px; margin:0 0 14px; background:rgba(255,255,255,.8); border:1px solid rgba(17,32,51,.08); border-radius:22px; backdrop-filter:blur(16px); box-shadow:0 14px 40px rgba(18,32,54,.08); overflow:auto; }
      .tab { border:0; background:transparent; color:#526276; border-radius:16px; padding:11px 14px; display:flex; gap:8px; align-items:center; font-weight:800; white-space:nowrap; }
      .tab.active { background:#112033; color:#fff; box-shadow:0 10px 24px rgba(17,32,51,.22); }
      .metrics { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:14px; margin-bottom:18px; }
      .metric { background:rgba(255,255,255,.94); border:1px solid rgba(17,32,51,.07); border-radius:22px; padding:18px; box-shadow:0 10px 30px rgba(18,32,54,.08); display:grid; gap:6px; min-width:0; }
      .metric ha-icon { color:#4ea1ff; }
      .metric span { color:#6b7b90; font-size:13px; font-weight:700; overflow:hidden; text-overflow:ellipsis; }
      .metric strong { font-size:26px; }
      .metric.warn ha-icon { color:#ff914d; }
      .metric.ok ha-icon { color:#34a853; }
      .grid.two { display:grid; grid-template-columns:1fr 1fr; gap:18px; }
      .panel-card { background:rgba(255,255,255,.96); border:1px solid rgba(17,32,51,.07); border-radius:26px; padding:22px; margin-bottom:18px; box-shadow:0 16px 45px rgba(18,32,54,.08); }
      .wide { grid-column:1 / -1; }
      .card-head { display:flex; justify-content:space-between; gap:16px; align-items:center; margin-bottom:16px; }
      .due,.location-compact,.invoice-row,.reading-row { display:flex; align-items:center; gap:14px; padding:14px; border-radius:18px; background:#f7f9fc; margin-top:10px; }
      .due { justify-content:space-between; border-left:5px solid #d8e2ef; }
      .due > div:first-child { min-width:0; }
      .due.soon { border-left-color:#ff914d; }
      .due.late { border-left-color:#e5484d; }
      .due span,.location-compact span,.invoice-main span,.invoice-meta span,.reading-main span,.reading-period span,.reading-current span { display:block; color:#6b7b90; font-size:13px; margin-top:3px; }
      .invoice-main .invoice-utility { color:#4f6f94; font-size:12px; font-weight:800; letter-spacing:.02em; }
      .invoice-main .invoice-location { color:#8aa7cf; font-size:12px; font-weight:900; letter-spacing:.02em; }
      .due-right { text-align:right; display:flex; align-items:baseline; justify-content:flex-end; gap:8px; flex-wrap:wrap; min-width:max-content; }
      .due-right b,.due-right small { display:inline-block; white-space:nowrap; }
      .due-right small { color:#6b7b90; font-weight:800; }
      .notification-toggles { margin-top:12px; }
      .location-compact { display:grid; grid-template-columns:42px minmax(0,1fr) auto; align-items:center; justify-content:initial; }
      .location-compact > b { margin-left:0; text-align:right; flex:0 0 auto; justify-self:end; }
      .location-icon,.provider-badge { width:42px; height:42px; border-radius:14px; display:grid; place-items:center; background:#e8f2ff; color:#2369bb; font-weight:900; flex:0 0 auto; }
      .financial-chip-row { grid-column:1 / -1; display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
      .financial-chip { display:inline-flex; align-items:center; gap:5px; padding:5px 8px; border-radius:999px; background:rgba(15,118,110,.09); color:#0f766e; font-size:11px; font-weight:800; line-height:1; white-space:nowrap; }
      .financial-chip ha-icon { width:14px; height:14px; color:currentColor; }
      .financial-chip span { display:inline; margin:0; color:inherit; font-size:11px; }
      .financial-chip strong { display:inline; font-size:11px; color:inherit; }
      .location-compact-main { min-width:0; width:100%; justify-self:start; flex:1 1 auto; text-align:left; }
      .location-compact-main strong { display:block; text-align:left; overflow-wrap:anywhere; }
      .summary-strip,.details-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; }
      .summary-strip div,.details-grid div { padding:16px; border-radius:18px; background:#f7f9fc; min-width:0; }
      .summary-strip strong,.details-grid strong { display:block; overflow-wrap:anywhere; }
      .summary-strip span,.details-grid span { color:#6b7b90; font-size:12px; text-transform:uppercase; letter-spacing:.08em; font-weight:800; }
      .location-title { width:100%; border:0; background:transparent; padding:0; display:flex; align-items:center; justify-content:space-between; text-align:left; color:inherit; }
      .location-title.static { cursor:default; }
      .location-total { display:flex; gap:8px; align-items:center; }
      .invoice-list,.reading-list { margin-top:16px; display:grid; gap:10px; }
      .invoice-toolbar.compact { display:flex; align-items:center; gap:12px; padding:14px 18px; border-radius:22px; }
      .invoice-toolbar label { color:#6b7b90; font-size:12px; text-transform:uppercase; letter-spacing:.08em; font-weight:900; }
      .invoice-toolbar select { border:1px solid rgba(17,32,51,.10); border-radius:14px; padding:10px 36px 10px 12px; background:#f7f9fc; color:#142033; font-weight:800; }
      .invoice-toolbar span { margin-left:auto; color:#6b7b90; font-weight:800; }
      .invoice-row { display:grid; grid-template-columns:42px minmax(170px,1fr) minmax(110px,135px) minmax(100px,120px) minmax(105px,130px) minmax(100px,125px) max-content max-content; align-items:center; column-gap:16px; }
      .invoice-row.warning { background:#fff5ec; }
      .invoice-details { display:contents; }
      .invoice-meta { min-width:0; }
      .invoice-meta strong { display:block; overflow-wrap:anywhere; }
      .invoice-details > .pill { justify-self:start; align-self:center; }
      .invoice-quick { display:none; }
      .invoice-toggle { display:none; width:42px; height:42px; border:1px solid rgba(17,32,51,.08); border-radius:14px; background:#fff; color:#112033; place-items:center; }
      .reading-row { display:grid; grid-template-columns:42px minmax(150px,1fr) minmax(210px,1.1fr) 110px auto; }
      .reading-controls { grid-column:2 / -1; display:grid; gap:10px; margin-top:2px; }
      .reading-control { display:grid; grid-template-columns:minmax(120px,1fr) minmax(120px,180px) auto; gap:10px; align-items:center; padding:12px; border-radius:16px; background:rgba(255,255,255,.72); border:1px solid rgba(17,32,51,.06); }
      .reading-control label { font-weight:900; }
      .reading-input { width:100%; border:1px solid rgba(17,32,51,.12); border-radius:14px; padding:11px 12px; font:inherit; background:#fff; color:#142033; outline:none; }
      .reading-input:focus { border-color:#4ea1ff; box-shadow:0 0 0 3px rgba(78,161,255,.16); }
      .reading-control .action-message { grid-column:1 / -1; margin-top:0; }
      .pill { display:inline-flex; align-items:center; justify-content:center; width:max-content; max-width:100%; padding:7px 12px; border-radius:999px; font-size:12px; font-weight:900; text-align:center; white-space:nowrap; line-height:1.15; }
      .pill.paid,.pill.credit,.pill.open { background:#e9f8ee; color:#14783c; }
      .pill.unpaid,.pill.closed { background:#fff0e6; color:#b55415; }
      .pill.unknown,.pill.missing { background:#edf1f7; color:#526276; }
      .refresh-wrap { display:flex; align-items:center; gap:8px; }
      .refresh-message { font-size:11px; font-weight:900; }
      .refresh-message.ok { color:#14783c; }
      .refresh-message.error { color:#b55415; }
      .row-action { width:38px; height:38px; border:1px solid rgba(17,32,51,.08); border-radius:14px; background:#fff; display:grid; place-items:center; color:#112033; }
      .row-action.busy ha-icon { animation:spin 1s linear infinite; }
      .row-action.disabled { color:#9aa7b7; background:#edf1f7; cursor:default; }
      .invoice-actions { display:flex; align-items:center; justify-content:flex-end; gap:8px; flex-wrap:nowrap; min-width:max-content; }
      .provider-app-action { width:38px; height:38px; border:1px solid rgba(17,32,51,.08); border-radius:14px; background:#fff; color:#112033; display:grid; place-items:center; box-shadow:none; }
      .provider-app-action ha-icon { width:20px; height:20px; }
      .provider-app-action span { display:none; }
      @keyframes spin { to { transform:rotate(360deg); } }
      .feature-note { display:flex; gap:16px; align-items:flex-start; padding:18px; border-radius:20px; background:#eef6ff; color:#23415f; }
      .feature-note ha-icon { color:#4ea1ff; flex:0 0 auto; }
      .feature-note p { color:#526276; margin-top:6px; }
      .feature-note.subtle { background:#f7f9fc; }
      .license-card { display:flex; gap:20px; align-items:center; background:linear-gradient(135deg,#ffffff,#edf7ff); }
      .license-shield { width:74px; height:74px; border-radius:24px; display:grid; place-items:center; background:#112033; color:#fff; }
      .license-shield ha-icon { width:34px; height:34px; }
      .license-form { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:12px; align-items:center; }
      .license-form input { width:100%; border:1px solid rgba(17,32,51,.12); border-radius:18px; padding:14px 16px; font:inherit; background:#f7f9fc; color:#142033; outline:none; }
      .license-form input:focus { border-color:#4ea1ff; box-shadow:0 0 0 3px rgba(78,161,255,.16); }
      .license-hint { margin:10px 2px 0; color:var(--muted); font-size:13px; line-height:1.45; }
      .license-reload-box { margin-top:18px; display:grid; grid-template-columns:minmax(0,1fr) auto; gap:16px; align-items:center; border-radius:22px; padding:18px; background:#f7f9fc; border:1px solid var(--border); }
      .license-reload-box h3 { margin:0 0 6px; font-size:17px; color:var(--text); }
      .license-reload-box p { margin:0; color:var(--muted); line-height:1.5; font-size:14px; }
      .ghost { border:1px solid rgba(17,32,51,.12); border-radius:999px; padding:11px 15px; background:#fff; color:#112033; font-weight:900; display:inline-flex; align-items:center; justify-content:center; gap:8px; white-space:nowrap; }
      .ghost.strong { background:#112033; color:#fff; border-color:#112033; box-shadow:0 12px 26px rgba(17,32,51,.16); }
      .ghost ha-icon { width:19px; height:19px; }
      .license-support-box { margin-top:18px; display:grid; grid-template-columns:auto 1fr; gap:16px; border-radius:22px; padding:18px; background:linear-gradient(135deg, var(--soft-blue), rgba(255,221,0,.18)); border:1px solid var(--border); }
      .license-support-icon { width:42px; height:42px; border-radius:16px; display:grid; place-items:center; background:var(--card); color:var(--accent); box-shadow:0 10px 24px rgba(17,32,51,.10); }
      .license-support-box h3 { margin:0 0 8px; font-size:17px; color:var(--text); }
      .license-support-box p { margin:0 0 8px; color:var(--muted); line-height:1.55; font-size:14px; }
      .license-support-box small { display:block; margin-top:10px; color:var(--muted); line-height:1.45; }
      .license-links { margin-top:14px; display:flex; justify-content:flex-start; }
      .bmc-button { display:inline-flex; align-items:center; gap:8px; padding:12px 16px; border-radius:999px; background:#ffdd00; color:#112033; text-decoration:none; font-weight:900; box-shadow:0 12px 28px rgba(17,32,51,.12); }
      .contact-card p { color:#526276; margin-bottom:18px; }
      .contact-actions { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; }
      .contact-action { display:flex; align-items:center; gap:10px; padding:16px; border-radius:18px; background:#f7f9fc; color:#142033; text-decoration:none; font-weight:900; border:1px solid rgba(17,32,51,.06); }
      .contact-action ha-icon { color:#4ea1ff; }

      .settings-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; }
      .settings-grid.compact { grid-template-columns:minmax(0,1fr); margin-bottom:12px; }
      .setting-block { display:grid; gap:14px; align-content:start; padding:18px; border-radius:20px; background:#f7f9fc; border:1px solid rgba(17,32,51,.06); }
      .setting-block h3 { margin:0 0 6px; font-size:18px; }
      .setting-block p,.setting-hint { color:#6b7b90; margin:0; line-height:1.45; }
      .setting-block select,.location-alias-row input { width:100%; border:1px solid rgba(17,32,51,.12); border-radius:16px; padding:13px 14px; font:inherit; background:#fff; color:#142033; outline:none; }
      .setting-block select { height:48px; min-height:48px; align-self:start; }
      .settings-list,.location-alias-list { display:grid; gap:12px; }
      .consumption-visibility-card { overflow:hidden; }
      .consumption-stats { display:grid; grid-template-columns:repeat(2,minmax(0,170px)); gap:12px; margin:16px 0 18px; }
      .consumption-stats div { border-radius:18px; padding:14px 16px; background:#f7f9fc; border:1px solid rgba(17,32,51,.06); }
      .consumption-stats span { display:block; color:#6b7b90; font-size:12px; font-weight:900; letter-spacing:.08em; text-transform:uppercase; }
      .consumption-stats strong { display:block; margin-top:4px; font-size:26px; line-height:1; color:#142033; }
      .consumption-sections { display:grid; gap:18px; }
      .consumption-section { display:grid; gap:10px; }
      .consumption-section-head { display:flex; justify-content:space-between; align-items:center; gap:12px; padding:0 2px; color:#142033; }
      .consumption-section-head strong { font-size:15px; }
      .consumption-section-head span { color:#6b7b90; font-size:12px; font-weight:800; }
      .consumption-point-list { display:grid; gap:10px; }
      .consumption-point-card { display:grid; grid-template-columns:auto minmax(0,1fr) auto; gap:14px; align-items:center; padding:14px; border-radius:20px; background:#f7f9fc; border:1px solid rgba(17,32,51,.07); }
      .consumption-point-card.ignored { opacity:.86; background:rgba(247,249,252,.72); }
      .consumption-provider-badge { width:42px; height:42px; border-radius:14px; display:grid; place-items:center; background:#e9f2ff; color:#1d67c2; font-weight:900; font-size:13px; }
      .consumption-point-main { min-width:0; display:grid; gap:7px; }
      .consumption-point-main strong { color:#142033; font-size:15px; line-height:1.2; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .consumption-point-chips { display:flex; flex-wrap:wrap; gap:6px; }
      .consumption-point-chips span { border-radius:999px; padding:4px 8px; background:#fff; color:#526276; font-size:12px; font-weight:800; border:1px solid rgba(17,32,51,.07); }
      .consumption-point-key { color:#6b7b90; font-size:12px; }
      .consumption-point-key summary { cursor:pointer; font-weight:800; }
      .consumption-point-key code { display:block; margin-top:6px; max-width:100%; overflow:auto; white-space:nowrap; border-radius:10px; padding:8px; background:#fff; border:1px solid rgba(17,32,51,.07); color:#526276; }
      .visibility-switch { display:flex; align-items:center; justify-content:flex-end; gap:10px; cursor:pointer; user-select:none; }
      .visibility-switch input { position:absolute; opacity:0; pointer-events:none; }
      .visibility-switch span { width:48px; height:28px; border-radius:999px; background:#dfe8f3; border:1px solid rgba(17,32,51,.10); position:relative; transition:.18s ease; }
      .visibility-switch span::after { content:""; position:absolute; width:22px; height:22px; border-radius:50%; background:#fff; top:2px; left:2px; box-shadow:0 4px 10px rgba(17,32,51,.18); transition:.18s ease; }
      .visibility-switch input:checked + span { background:#4ea1ff; border-color:#4ea1ff; }
      .visibility-switch input:checked + span::after { transform:translateX(20px); }
      .visibility-switch em { min-width:68px; color:#526276; font-style:normal; font-weight:900; font-size:13px; }
      .setting-toggle { display:flex; gap:12px; align-items:flex-start; padding:16px; border-radius:18px; background:#f7f9fc; border:1px solid rgba(17,32,51,.06); cursor:pointer; }
      .setting-toggle input { width:22px; height:22px; accent-color:#4ea1ff; margin-top:1px; flex:0 0 auto; }
      .setting-toggle span { display:grid; gap:3px; }
      .setting-toggle small,.setting-field small,.location-alias-row small { color:#6b7b90; line-height:1.35; }
      .setting-field { display:grid; gap:6px; padding:16px; border-radius:18px; background:#f7f9fc; border:1px solid rgba(17,32,51,.06); }
      .setting-field span { font-weight:900; color:#526276; }
      .setting-field input { width:100%; box-sizing:border-box; border:1px solid rgba(17,32,51,.12); border-radius:16px; padding:13px 14px; font:inherit; background:#fff; color:#142033; outline:none; }
      .setting-field input:focus { border-color:#4ea1ff; box-shadow:0 0 0 3px rgba(78,161,255,.16); }
      .location-alias-row { display:grid; grid-template-columns:minmax(0,1fr) minmax(220px,.7fr); gap:14px; align-items:center; padding:16px; border-radius:18px; background:#f7f9fc; border:1px solid rgba(17,32,51,.06); }
      .location-alias-row span { display:grid; gap:4px; }
      .provider-status-list { display:grid; gap:10px; }
      .provider-status-row { display:grid; grid-template-columns:minmax(0,1fr) auto auto; gap:10px; align-items:center; padding:14px; border-radius:16px; background:#f7f9fc; }
      .provider-status-row div { display:grid; gap:2px; }
      .provider-status-row span:not(.pill) { color:#6b7b90; font-size:13px; }
      .action-message { margin-top:14px; border-radius:16px; padding:12px 14px; font-weight:700; }
      .action-message.ok { background:#e9f8ee; color:#14783c; }
      .action-message.error { background:#fff0e6; color:#b55415; }
      .empty { color:#6b7b90; background:#f7f9fc; border-radius:18px; padding:18px; }

      .support-note { display:flex; gap:12px; align-items:flex-start; border-radius:18px; padding:14px 16px; background:var(--soft-blue); color:var(--muted); font-size:14px; line-height:1.55; margin:14px 0 18px; }
      .support-note ha-icon { color:var(--accent); flex:0 0 auto; margin-top:1px; }
      button[disabled] { opacity:.68; cursor:not-allowed; }

      @media (prefers-color-scheme: dark) {
        :host {
          background:
            radial-gradient(circle at -80px -120px, rgba(78,161,255,.16) 0, rgba(78,161,255,.08) 210px, transparent 212px),
            linear-gradient(180deg, #0b1220 0%, #0f172a 100%);
          color:#edf3fb;
        }
        .hero-content { box-shadow:0 24px 70px rgba(0,0,0,.34); border-color:rgba(255,255,255,.10); }
        .hero-card,.panel-card,.metric,.tabs {
          background:#172033;
          color:#edf3fb;
          border-color:rgba(255,255,255,.08);
          box-shadow:0 18px 48px rgba(0,0,0,.26);
        }
        .hero-card::before { background:rgba(78,161,255,.16); }
        .tab { color:#a8b3c4; }
        .tab.active { background:#4ea1ff; color:#ffffff; box-shadow:0 12px 28px rgba(78,161,255,.26); }
        .hero-card-label,.hero-card small,.metric span,.due span,.location-compact span,.invoice-main span,.invoice-meta span,.reading-main span,.reading-period span,.reading-current span,.details-grid span,.summary-strip span,.empty,.contact-card p,.feature-note p,.provider-status-row span:not(.pill),.invoice-toolbar label,.invoice-toolbar span,.setting-block p,.setting-hint,.setting-toggle small,.setting-field small,.setting-field span,.location-alias-row small {
          color:#a8b3c4;
        }
        .invoice-row,.reading-row,.due,.location-compact,.details-grid div,.summary-strip div,.contact-action,.provider-status-row,.empty,.setting-block,.setting-toggle,.setting-field,.location-alias-row,.consumption-stats div,.consumption-point-card {
          background:#111a2b;
          color:#edf3fb;
          border-color:rgba(255,255,255,.08);
        }
        .consumption-section-head,.consumption-stats strong,.consumption-point-main strong { color:#edf3fb; }
        .consumption-stats span,.consumption-section-head span,.consumption-point-key,.visibility-switch em { color:#a8b3c4; }
        .consumption-provider-badge { background:#dcecff; color:#1d67c2; }
        .consumption-point-chips span,.consumption-point-key code { background:#0f172a; color:#c8d3e2; border-color:rgba(255,255,255,.08); }
        .consumption-point-card.ignored { background:rgba(17,26,43,.68); opacity:.82; }
        .invoice-row.warning { background:#251b12; }
        .invoice-toolbar select,.license-form input,.reading-input,.setting-block select,.setting-field input,.location-alias-row input {
          background:#0f172a;
          color:#edf3fb;
          border-color:rgba(255,255,255,.14);
        }
        .invoice-toolbar select:focus,.license-form input:focus,.reading-input:focus,.setting-block select:focus,.setting-field input:focus,.location-alias-row input:focus {
          border-color:#4ea1ff;
          box-shadow:0 0 0 3px rgba(78,161,255,.18);
        }
        .reading-control {
          background:#172033;
          color:#edf3fb;
          border-color:rgba(255,255,255,.10);
        }
        .row-action,.invoice-toggle {
          background:#ffffff;
          color:#112033;
          border-color:rgba(255,255,255,.16);
        }
        .row-action.disabled { background:#202b3f; color:#7f8da0; }
        .provider-app-action { background:#f7f9fc; color:#112033; border-color:rgba(210,219,232,.22); box-shadow:none; }
        .feature-note { background:rgba(78,161,255,.12); color:#dcecff; }
        .feature-note.subtle { background:#111a2b; }
        .license-card { background:linear-gradient(135deg,#172033,#111a2b); }
        .license-reload-box { background:#111a2b; border-color:rgba(255,255,255,.08); }
        .ghost { background:#172033; color:#edf3fb; border-color:rgba(255,255,255,.14); }
        .ghost.strong { background:#4ea1ff; color:#fff; border-color:#4ea1ff; box-shadow:0 12px 26px rgba(78,161,255,.18); }
        .license-shield { background:#4ea1ff; }
        .contact-action { color:#edf3fb; }
        .pill.paid,.pill.credit,.pill.open { background:#dbf7e6; color:#14783c; }
        .pill.unpaid,.pill.closed { background:#ffe6d4; color:#a0440f; }
        .pill.unknown,.pill.missing { background:#263449; color:#d2dbe8; }
        .financial-chip { background:rgba(45,212,191,.13); color:#99f6e4; }
      }
      @media (max-width: 900px) {
        .dashboard-back-button { right:18px; bottom:18px; padding:9px 12px; }
        .hero,.grid.two { grid-template-columns:1fr; }
        .invoice-toolbar.compact { display:grid; grid-template-columns:auto minmax(0,1fr) auto minmax(0,1fr) auto; gap:10px; align-items:center; }
        .invoice-toolbar select { width:100%; min-width:0; }
        .invoice-row { grid-template-columns:42px minmax(0,1fr) 44px; align-items:start; }
        .invoice-main { grid-column:2; }
        .invoice-quick { display:flex; align-items:center; gap:10px; flex-wrap:wrap; grid-column:2; margin-top:10px; }
        .invoice-quick strong { font-size:16px; }
        .invoice-details { display:none; grid-column:2 / 4; grid-template-columns:1fr; gap:10px; margin-top:12px; padding-top:12px; border-top:1px solid rgba(17,32,51,.07); }
        .invoice-row.expanded .invoice-details { display:grid; }
        .invoice-details .invoice-meta,.invoice-details .pill,.invoice-details .row-action,.invoice-details .invoice-actions { justify-self:start; }
        .invoice-actions { align-items:flex-start; justify-content:flex-start; flex-wrap:wrap; min-width:0; }
        .invoice-toggle { display:grid; grid-column:3; grid-row:1; }
        .reading-row { grid-template-columns:42px 1fr; }
        .reading-period,.reading-current,.reading-row .pill,.reading-controls { grid-column:2; justify-self:stretch; }
        .reading-control { grid-template-columns:1fr; }
        .summary-strip,.details-grid { grid-template-columns:1fr; }
        .contact-actions { grid-template-columns:1fr; }
        .provider-status-row { grid-template-columns:1fr; align-items:start; }
        .settings-grid,.location-alias-row,.consumption-point-card { grid-template-columns:1fr; }
        .consumption-stats { grid-template-columns:repeat(2,minmax(0,1fr)); }
        .visibility-switch { justify-content:space-between; }
      }
      @media (max-width: 560px) {
        :host { background:radial-gradient(circle at -80px -120px,#07111f 0,#10223d 210px,transparent 212px),linear-gradient(180deg,#eef4fb 0%,#f7f9fc 100%); }
        .wrap { padding:12px 10px 28px; overflow-x:hidden; }
        .hero-content,.hero-card,.panel-card { border-radius:22px; padding:18px; }
        .hero-content { padding-bottom:70px; }
        .hero { gap:12px; }
        .brand-row { align-items:center; gap:12px; padding-right:0; min-height:70px; }
        .forge-lockup { top:auto; right:18px; bottom:18px; font-size:10px; }
        .forge-lockup span { display:inline; }
        .utility-logo { width:58px; height:58px; border-radius:18px; }
        .forge-logo { width:30px; height:30px; border-radius:10px; }
        h1 { font-size:32px; padding-right:0; }
        .tabs { margin-top:4px; justify-content:space-between; overflow-x:auto; }
        .tab { padding:10px 12px; }
        .tab span { display:none; }
        .metrics { grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; }
        .metric { padding:10px 8px; border-radius:16px; min-height:82px; }
        .metric ha-icon { width:20px; height:20px; }
        .metric span { font-size:10px; }
        .metric strong { font-size:20px; }
        .invoice-toolbar.compact { grid-template-columns:minmax(0,.72fr) minmax(0,1.28fr); gap:8px 10px; padding:16px; overflow:hidden; }
        .invoice-toolbar label { font-size:10px; align-self:center; }
        .invoice-toolbar select { width:100%; max-width:100%; min-width:0; padding:10px 30px 10px 12px; font-size:14px; }
        .invoice-toolbar span { grid-column:1 / -1; margin-left:0; text-align:right; }
        .license-form { grid-template-columns:1fr; }
        .license-reload-box { grid-template-columns:1fr; padding:16px; }
        .license-reload-box .ghost { width:100%; }
        .license-support-box { grid-template-columns:1fr; padding:16px; }
        .license-support-icon { width:38px; height:38px; }
        .license-links { justify-content:stretch; }
        .bmc-button { width:100%; justify-content:center; text-align:center; }
      }
      @media (prefers-color-scheme: dark) and (max-width: 560px) {
        :host {
          background:
            radial-gradient(circle at -80px -120px, rgba(78,161,255,.16) 0, rgba(78,161,255,.08) 210px, transparent 212px),
            linear-gradient(180deg, #0b1220 0%, #0f172a 100%);
        }
      }
    `;
  }


  _sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  _parsePositiveNumber(value) {
    const parsed = Number(String(value ?? "").trim().replace(",", "."));
    return Number.isFinite(parsed) ? parsed : NaN;
  }

  async _waitForNumberState(entityId, expectedValue, timeoutMs = 7000) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      const current = this._parsePositiveNumber(this._hass?.states?.[entityId]?.state);
      if (Number.isFinite(current) && Math.abs(current - expectedValue) < 0.0001) return true;
      await this._sleep(350);
    }
    return false;
  }

  async _submitReading(wrapper) {
    const providerKey = String(wrapper?.getAttribute("data-provider") || "").trim().toLowerCase();
    const numberEntityId = wrapper?.getAttribute("data-number-entity") || "";
    const buttonEntityId = wrapper?.getAttribute("data-button-entity") || "";
    const currentValue = wrapper?.getAttribute("data-current-value") || "";
    const input = wrapper?.querySelector(".reading-input");
    const valueText = String(input?.value || "").trim();
    const actionKey = `reading__${buttonEntityId || numberEntityId}`;
    const numericValue = this._parsePositiveNumber(valueText);

    if (!valueText || !Number.isFinite(numericValue) || numericValue <= 0) {
      this._actions.set(actionKey, { status: "error", message: "Introdu o valoare numerică validă pentru index." });
      this._render();
      return;
    }

    const currentNumeric = this._parsePositiveNumber(currentValue);
    if (Number.isFinite(currentNumeric) && currentNumeric > 0 && numericValue < currentNumeric) {
      this._actions.set(actionKey, { status: "error", message: `Valoarea introdusă este mai mică decât indexul curent (${currentNumeric}).` });
      this._render();
      return;
    }

    this._actions.set(actionKey, { status: "busy", message: "" });
    this._render();

    try {
      if (providerKey === "engie" || providerKey === "apa_canal" || providerKey === "apa_brasov" || providerKey === "apa_oradea" || providerKey === "apa_galati" || providerKey === "hidro_prahova") {
        await this._hass.callService("utilitati_romania", "submit_reading", {
          provider: providerKey,
          entry_id: String(wrapper?.getAttribute("data-entry-id") || ""),
          id_cont: String(wrapper?.getAttribute("data-id-cont") || ""),
          id_contract: String(wrapper?.getAttribute("data-id-contract") || ""),
          value: numericValue,
        });
      } else {
        if (!numberEntityId || !buttonEntityId) throw new Error("Entitățile pentru transmiterea indexului nu sunt disponibile.");
        if (providerKey === "eon") {
          const wrongProviderPattern = /(hidro|hidroelectrica|myelectrica|apa_canal|apa_brasov|apa_oradea|apa_galati|hidro_prahova|apacanal|ebloc)/i;
          if (wrongProviderPattern.test(numberEntityId) || wrongProviderPattern.test(buttonEntityId)) throw new Error("Panoul a identificat o entitate de la alt furnizor pentru E.ON. Reîncarcă pagina și verifică entitățile.");
        }
        await this._hass.callService("number", "set_value", { entity_id: numberEntityId, value: numericValue });
        const synced = await this._waitForNumberState(numberEntityId, numericValue, providerKey === "eon" ? 15000 : 7000);
        if (!synced) throw new Error(`Valoarea introdusă nu a fost confirmată încă de Home Assistant pentru ${numberEntityId}. Reîncearcă după câteva secunde.`);
        await this._hass.callService("button", "press", { entity_id: buttonEntityId });
      }
      this._readingDrafts.delete(buttonEntityId || numberEntityId);
      this._actions.set(actionKey, { status: "ok", message: "Comanda de transmitere a indexului a fost trimisă. Verifică ulterior portalul furnizorului sau următorul refresh pentru confirmarea finală." });
    } catch (err) {
      this._actions.set(actionKey, { status: "error", message: err?.message || "Transmiterea indexului a eșuat." });
    }
    this._render();
  }

  _bindEvents() {
    this.shadowRoot.querySelectorAll("[data-tab]").forEach((button) => {
      button.addEventListener("click", () => {
        this._activeTab = button.getAttribute("data-tab") || "overview";
        this._render();
      });
    });
    this.shadowRoot.querySelectorAll("[data-toggle-location]").forEach((button) => {
      button.addEventListener("click", () => {
        const key = button.getAttribute("data-toggle-location");
        if (this._expandedLocations.has(key)) this._expandedLocations.delete(key);
        else this._expandedLocations.add(key);
        this._render();
      });
    });
    this.shadowRoot.querySelectorAll("[data-toggle-invoice]").forEach((button) => {
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        const key = button.getAttribute("data-toggle-invoice");
        if (this._expandedInvoices.has(key)) this._expandedInvoices.delete(key);
        else this._expandedInvoices.add(key);
        this._render();
      });
    });
    const invoiceGrouping = this.shadowRoot.querySelector("[data-invoice-grouping]");
    if (invoiceGrouping) {
      ["focus", "mousedown", "pointerdown", "touchstart"].forEach((eventName) => {
        invoiceGrouping.addEventListener(eventName, () => this._holdRenderBriefly(4500));
      });
      invoiceGrouping.addEventListener("change", (event) => {
        this._setInvoiceGrouping(event.target.value);
        this._interactiveUntil = 0;
        this._render();
      });
    }
    const invoiceFilter = this.shadowRoot.querySelector("[data-invoice-filter]");
    if (invoiceFilter) {
      ["focus", "mousedown", "pointerdown", "touchstart"].forEach((eventName) => {
        invoiceFilter.addEventListener(eventName, () => this._holdRenderBriefly(4500));
      });
      invoiceFilter.addEventListener("change", (event) => {
        this._setInvoiceFilter(event.target.value);
        this._interactiveUntil = 0;
        this._render();
      });
    }

    const mobileDeviceSelect = this.shadowRoot.querySelector("[data-mobile-device-select]");
    if (mobileDeviceSelect) {
      ["focus", "mousedown", "pointerdown", "touchstart"].forEach((eventName) => mobileDeviceSelect.addEventListener(eventName, () => this._holdRenderBriefly(4500)));
      mobileDeviceSelect.addEventListener("change", async (event) => {
        const entityId = mobileDeviceSelect.getAttribute("data-entity-id");
        const option = event.target.value;
        this._actions.set("settings", { status: "busy", message: "Se salvează dispozitivul mobil..." });
        this._holdRenderBriefly(1200);
        this._render();
        try {
          await this._hass.callService("select", "select_option", { entity_id: entityId, option });
          this._actions.set("settings", { status: "ok", message: "Dispozitivul mobil a fost salvat." });
        } catch (err) {
          this._actions.set("settings", { status: "error", message: err?.message || "Nu am putut salva dispozitivul mobil." });
        }
        this._interactiveUntil = 0;
        this._render();
      });
    }
    const mobileNotificationSelect = this.shadowRoot.querySelector("[data-mobile-notification-select]");
    if (mobileNotificationSelect) {
      ["focus", "mousedown", "pointerdown", "touchstart"].forEach((eventName) => mobileNotificationSelect.addEventListener(eventName, () => this._holdRenderBriefly(4500)));
      mobileNotificationSelect.addEventListener("change", async (event) => {
        const entityId = mobileNotificationSelect.getAttribute("data-entity-id");
        const option = event.target.value;
        this._actions.set("settings", { status: "busy", message: "Se salvează telefonul pentru notificări..." });
        this._holdRenderBriefly(1200);
        this._render();
        try {
          await this._hass.callService("select", "select_option", { entity_id: entityId, option });
          this._actions.set("settings", { status: "ok", message: "Telefonul pentru notificări a fost salvat." });
        } catch (err) {
          this._actions.set("settings", { status: "error", message: err?.message || "Nu am putut salva telefonul pentru notificări." });
        }
        this._interactiveUntil = 0;
        this._render();
      });
    }
    this.shadowRoot.querySelectorAll("[data-consumption-visibility]").forEach((control) => {
      const applyVisibility = async () => {
        if (control.disabled) return;
        const key = control.getAttribute("data-consumption-visibility") || "";
        const ignored = control.matches("input[type='checkbox']") ? !control.checked : control.getAttribute("data-ignored") === "true";
        const label = control.getAttribute("data-label") || "locul de consum";
        this._actions.set("settings", { status: "busy", message: ignored ? `Se ignoră ${label}...` : `Se reactivează ${label}...` });
        this._holdRenderBriefly(1200);
        this._render();
        try {
          await this._hass.callService("utilitati_romania", "set_consumption_point_visibility", { cheie: key, ignored });
          this._actions.set("settings", { status: "ok", message: ignored ? "Locul de consum a fost ignorat. Va dispărea din dashboard după actualizarea senzorului agregat." : "Locul de consum a fost reactivat." });
        } catch (err) {
          this._actions.set("settings", { status: "error", message: err?.message || "Nu am putut salva vizibilitatea locului de consum." });
        }
        this._interactiveUntil = 0;
        this._render();
      };
      control.addEventListener(control.matches("input[type='checkbox']") ? "change" : "click", applyVisibility);
    });
    this.shadowRoot.querySelectorAll("[data-setting-toggle]").forEach((input) => {
      input.addEventListener("change", () => {
        const prefs = this._notificationPreferences();
        prefs[input.getAttribute("data-setting-toggle")] = input.checked;
        this._saveJsonPreference("notification_preferences", prefs);
      });
    });
    this.shadowRoot.querySelectorAll("[data-dashboard-pref]").forEach((input) => {
      input.addEventListener("change", () => {
        const prefs = this._dashboardPreferences();
        prefs[input.getAttribute("data-dashboard-pref")] = input.checked;
        this._saveJsonPreference("dashboard_preferences", prefs);
      });
    });
    this.shadowRoot.querySelectorAll("[data-dashboard-pref-text]").forEach((input) => {
      input.addEventListener("focus", () => this._holdRenderBriefly(9000));
      input.addEventListener("input", (event) => {
        const key = input.getAttribute("data-dashboard-pref-text");
        const value = event.target.value || "";
        const prefs = this._dashboardPreferences();
        prefs[key] = value;
        this._dashboardDrafts.set(key, value);
        this._saveJsonPreference("dashboard_preferences", prefs);
        this._holdRenderBriefly(9000);
      });
      input.addEventListener("blur", () => {
        const key = input.getAttribute("data-dashboard-pref-text");
        const prefs = this._dashboardPreferences();
        prefs[key] = input.value || "";
        this._saveJsonPreference("dashboard_preferences", prefs);
        window.setTimeout(() => this._dashboardDrafts.delete(key), 200);
      });
    });
    this.shadowRoot.querySelectorAll("[data-location-alias]").forEach((input) => {
      input.addEventListener("focus", () => this._holdRenderBriefly(4500));
      input.addEventListener("input", (event) => { this._holdRenderBriefly(4500); this._settingsDrafts.set(`alias__${input.getAttribute("data-location-alias")}`, event.target.value || ""); });
    });
    this.shadowRoot.querySelectorAll("[data-billing-group]").forEach((input) => {
      input.addEventListener("focus", () => this._holdRenderBriefly(4500));
      input.addEventListener("input", (event) => { this._holdRenderBriefly(4500); this._settingsDrafts.set(`billing__${input.getAttribute("data-billing-group")}`, event.target.value || ""); });
    });
    const saveBillingGroups = this.shadowRoot.querySelector("[data-save-billing-groups]");
    if (saveBillingGroups) {
      saveBillingGroups.addEventListener("click", async () => {
        if (saveBillingGroups.disabled) return;
        const inputs = Array.from(this.shadowRoot.querySelectorAll("[data-billing-group]"));
        this._actions.set("settings", { status: "busy", message: "Se salvează grupările facturilor..." });
        this._render();
        try {
          for (const input of inputs) {
            const entityId = input.getAttribute("data-billing-group");
            const value = String(input.value || "").trim();
            const current = this._hass?.states?.[entityId]?.state;
            const normalizedCurrent = current && !["unknown", "unavailable"].includes(current) ? String(current).trim() : "";
            if (value === normalizedCurrent) continue;
            await this._hass.callService("text", "set_value", { entity_id: entityId, value });
          }
          for (const input of inputs) this._settingsDrafts.delete(`billing__${input.getAttribute("data-billing-group")}`);
          this._actions.set("settings", { status: "ok", message: "Grupările facturilor au fost salvate. Datele se vor regrupa după următorul refresh al senzorului agregat." });
        } catch (err) {
          this._actions.set("settings", { status: "error", message: err?.message || "Nu am putut salva grupările facturilor." });
        }
        this._interactiveUntil = 0;
        this._render();
      });
    }
    const saveLocationAliases = this.shadowRoot.querySelector("[data-save-location-aliases]");
    if (saveLocationAliases) {
      saveLocationAliases.addEventListener("click", () => {
        if (saveLocationAliases.disabled) return;
        const aliases = this._locationAliases();
        this.shadowRoot.querySelectorAll("[data-location-alias]").forEach((input) => {
          const key = input.getAttribute("data-location-alias");
          const value = String(input.value || "").trim();
          if (value) aliases[key] = value;
          else delete aliases[key];
        });
        this._saveJsonPreference("location_aliases", aliases);
        this._settingsDrafts.clear();
        this._actions.set("settings", { status: "ok", message: "Denumirile afișate au fost salvate pentru acest dashboard." });
        this._render();
      });
    }

    const dashboardBack = this.shadowRoot.querySelector("[data-dashboard-back]");
    if (dashboardBack) dashboardBack.addEventListener("click", (event) => { event.preventDefault(); this._navigateDashboardBack(); });
    const saveNotificationSettings = this.shadowRoot.querySelector("[data-save-notification-settings]");
    if (saveNotificationSettings) {
      saveNotificationSettings.addEventListener("click", async () => {
        if (saveNotificationSettings.disabled) return;
        const prefs = this._notificationPreferences();
        this._actions.set("settings", { status: "busy", message: "Se salvează notificările..." });
        this._render();
        try {
          await this._hass.callService("utilitati_romania", "set_notification_preferences", prefs);
          this._actions.set("settings", { status: "ok", message: "Preferințele de notificare au fost salvate." });
        } catch (err) {
          this._actions.set("settings", { status: "error", message: err?.message || "Preferințele au fost salvate local, dar nu au putut fi trimise către backend." });
        }
        this._render();
      });
    }
    this.shadowRoot.querySelectorAll("[data-reading-control]").forEach((wrapper) => {
      const input = wrapper.querySelector(".reading-input");
      const buttonEntityId = wrapper.getAttribute("data-button-entity") || wrapper.getAttribute("data-number-entity") || "";
      if (input && buttonEntityId) {
        input.addEventListener("focus", () => this._holdRenderBriefly(4500));
        input.addEventListener("input", (event) => { this._holdRenderBriefly(4500); this._readingDrafts.set(buttonEntityId, event.target.value || ""); });
        input.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); this._submitReading(wrapper); } });
      }
      const submit = wrapper.querySelector("[data-reading-submit]");
      if (submit) submit.addEventListener("click", (event) => { event.stopPropagation(); this._submitReading(wrapper); });
    });
    this.shadowRoot.querySelectorAll("[data-open-provider]").forEach((button) => {
      button.addEventListener("click", async (event) => {
        event.stopPropagation();
        const provider = button.getAttribute("data-open-provider");
        if (!provider || button.disabled) return;
        const key = `open_provider__${provider}`;
        this._actions.set(key, { status: "busy" });
        this._render();
        try {
          await this._hass.callService("utilitati_romania", "open_provider", { provider });
          this._actions.set(key, { status: "ok" });
        } catch (err) {
          this._actions.set(key, { status: "error", message: err?.message || "Nu am putut deschide aplicația furnizorului." });
        }
        this._render();
      });
    });
    this.shadowRoot.querySelectorAll("[data-refresh-entity]").forEach((button) => {
      button.addEventListener("click", async () => {
        const entityId = button.getAttribute("data-refresh-entity");
        const key = button.getAttribute("data-action-key") || `refresh__${entityId}`;
        if (!entityId || button.disabled) return;
        this._actions.set(key, { status: "busy" });
        this._render();
        try {
          await this._hass.callService("button", "press", { entity_id: entityId });
          this._actions.set(key, { status: "ok" });
        } catch (err) {
          this._actions.set(key, { status: "error", message: err?.message || "Actualizarea a eșuat." });
        }
        this._render();
      });
    });
    const copyDiagnostics = this.shadowRoot.querySelector("[data-copy-diagnostics]");
    if (copyDiagnostics) {
      copyDiagnostics.addEventListener("click", async () => {
        const payload = JSON.stringify(this._diagnosticPayload(this._summary()), null, 2);
        this._actions.set("copy_diagnostics", { status: "busy", message: "" });
        this._render();
        try {
          await navigator.clipboard.writeText(payload);
          this._actions.set("copy_diagnostics", { status: "ok", message: "Diagnosticul a fost copiat în clipboard." });
        } catch (_err) {
          this._actions.set("copy_diagnostics", { status: "error", message: "Nu am putut copia automat. Selectează și copiază manual din consola browserului." });
          console.info("Utilități România diagnostic", payload);
        }
        this._render();
      });
    }
    const verifyLicense = this.shadowRoot.querySelector("[data-verify-license]");
    if (verifyLicense) {
      verifyLicense.addEventListener("click", async () => {
        if (verifyLicense.disabled) return;
        const verifyEntity = this._adminVerifyLicenseEntity();
        const beforeLicense = this._licenseStates();
        this._actions.set("verify_license", { status: "busy", message: "Se verifică licența curentă..." });
        this._render();
        try {
          if (!verifyEntity?.entity_id) throw new Error("Nu am găsit butonul de verificare a licenței.");
          await this._callServiceWithTimeout("button", "press", { entity_id: verifyEntity.entity_id }, 15000);
          await this._sleep(1200);
          const updatedLicense = this._licenseStates();
          this._actions.set("verify_license", {
            status: "ok",
            message: `Licența curentă a fost verificată. Status: ${updatedLicense.status || "necunoscut"}${updatedLicense.plan && updatedLicense.plan !== "—" ? ` / ${updatedLicense.plan}` : ""}${updatedLicense.key && updatedLicense.key !== "—" ? ` · licență: ${updatedLicense.key}` : ""}.`,
          });
        } catch (err) {
          if (err?.message === "timeout") {
            await this._sleep(1800);
            const updatedLicense = this._licenseStates();
            const changed = String(updatedLicense.status) !== String(beforeLicense.status) || String(updatedLicense.plan) !== String(beforeLicense.plan) || String(updatedLicense.message) !== String(beforeLicense.message);
            this._actions.set("verify_license", {
              status: changed ? "ok" : "error",
              message: changed
                ? `Licența curentă a fost verificată. Status: ${updatedLicense.status || "necunoscut"}${updatedLicense.plan && updatedLicense.plan !== "—" ? ` / ${updatedLicense.plan}` : ""}.`
                : "Verificarea a fost trimisă, dar Home Assistant nu a confirmat rapid finalizarea. Verifică statusul de mai sus după refresh.",
            });
          } else {
            this._actions.set("verify_license", { status: "error", message: err?.message || "Verificarea licenței a eșuat." });
          }
        }
        this._render();
      });
    }

    const reloadProviders = this.shadowRoot.querySelector("[data-reload-providers]");
    if (reloadProviders) {
      reloadProviders.addEventListener("click", async () => {
        if (reloadProviders.disabled) return;
        const reloadEntity = this._adminReloadEntity();
        this._actions.set("reload_providers", { status: "busy", message: "Am pornit reîncărcarea furnizorilor..." });
        this._render();
        try {
          if (reloadEntity?.entity_id) {
            await this._callServiceWithTimeout("button", "press", { entity_id: reloadEntity.entity_id }, 3500);
          } else {
            await this._callServiceWithTimeout("utilitati_romania", "reload_all", {}, 3500);
          }
          this._actions.set("reload_providers", { status: "ok", message: "Reîncărcarea furnizorilor a fost pornită. Pentru furnizorii lenți, datele pot apărea după câteva zeci de secunde." });
        } catch (err) {
          if (err?.message === "timeout") {
            this._actions.set("reload_providers", { status: "ok", message: "Reîncărcarea furnizorilor a fost pornită. Unele subintegrări, cum ar fi e-bloc, pot dura mai mult." });
          } else {
            this._actions.set("reload_providers", { status: "error", message: err?.message || "Nu am putut porni reîncărcarea furnizorilor." });
          }
        }
        this._render();
      });
    }

    const licenseInput = this.shadowRoot.querySelector("#license-input");
    if (licenseInput) {
      licenseInput.addEventListener("focus", () => this._holdRenderBriefly(4500));
      licenseInput.addEventListener("input", (event) => { this._holdRenderBriefly(4500); this._licenseDraft = event.target.value || ""; });
    }
    const applyLicense = this.shadowRoot.querySelector("[data-apply-license]");
    if (applyLicense) {
      applyLicense.addEventListener("click", async () => {
        if (applyLicense.disabled) return;
        const code = String(this.shadowRoot.querySelector("#license-input")?.value || this._licenseDraft || "").trim();
        if (!code) {
          this._actions.set("license", { status: "error", message: "Introdu mai întâi codul de licență." });
          this._render();
          return;
        }
        const entities = this._licenseEntities();
        const beforeLicense = this._licenseStates();
        this._actions.set("license", { status: "busy", message: "" });
        this._render();
        try {
          await this._callServiceWithTimeout("text", "set_value", { entity_id: entities.text, value: code }, 8000);
          await this._callServiceWithTimeout("button", "press", { entity_id: entities.button }, 15000);
          await this._sleep(1200);
          const updatedLicense = this._licenseStates();
          this._licenseDraft = "";
          this._actions.set("license", {
            status: "ok",
            message: `Licența a fost verificată. Status actual: ${updatedLicense.status || "necunoscut"}${updatedLicense.plan && updatedLicense.plan !== "—" ? ` / ${updatedLicense.plan}` : ""}${updatedLicense.key && updatedLicense.key !== "—" ? ` · licență activă: ${updatedLicense.key}` : ""}. Dacă furnizorii au rămas indisponibili după expirarea trialului, folosește butonul „Reîncarcă furnizorii”.`,
          });
        } catch (err) {
          if (err?.message === "timeout") {
            await this._sleep(1800);
            const updatedLicense = this._licenseStates();
            const changed = String(updatedLicense.status) !== String(beforeLicense.status) || String(updatedLicense.plan) !== String(beforeLicense.plan) || String(updatedLicense.message) !== String(beforeLicense.message);
            this._actions.set("license", {
              status: "ok",
              message: changed
                ? `Licența a fost verificată. Status actual: ${updatedLicense.status || "necunoscut"}${updatedLicense.plan && updatedLicense.plan !== "—" ? ` / ${updatedLicense.plan}` : ""}${updatedLicense.key && updatedLicense.key !== "—" ? ` · licență activă: ${updatedLicense.key}` : ""}. Dacă furnizorii au rămas indisponibili după expirarea trialului, folosește butonul „Reîncarcă furnizorii”.`
                : "Comanda de verificare a fost trimisă. Dacă statusul de sus nu se actualizează în câteva secunde, fă refresh și verifică mesajul licenței.",
            });
          } else {
            this._actions.set("license", { status: "error", message: err?.message || "Aplicarea licenței a eșuat." });
          }
        }
        this._render();
      });
    }
  }

  _render() {
    if (!this.shadowRoot) return;
    if (!this._hass) {
      this.shadowRoot.innerHTML = `<style>${this._styles()}</style><div class="wrap"><section class="panel-card"><div class="empty">Se încarcă datele...</div></section></div>`;
      return;
    }
    const summary = this._summary();
    this.shadowRoot.innerHTML = `
      <style>${this._styles()}</style>
      <div class="wrap">
        ${this._renderHero(summary.attrs)}
        ${this._renderTabs()}
        ${this._renderMetrics(summary.attrs, summary.locations)}
        <main>${this._renderContent(summary)}</main>
      </div>
    `;
    this._bindEvents();
  }
}

if (!customElements.get("utilitati-romania-panel")) {
  customElements.define("utilitati-romania-panel", UtilitatiRomaniaPanel);
}
