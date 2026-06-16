const UTILITATI_ROMANIA_FRONTEND_VERSION = "1.9.2b20";

class UtilitatiRomaniaFacturiCard extends HTMLElement {
  setConfig(config) {
    const rawConfig = config || {};
    this._config = {
      title: "Facturi utilități",
      entity: null,
      show_header: true,
      show_summary: true,
      only_unpaid: false,
      show_paid: true,
      show_license: true,
      invoice_grouping: "location",
      ...rawConfig,
    };

    if (!this._expanded) this._expanded = {};
    if (typeof this._licenseExpanded !== "boolean") this._licenseExpanded = false;
    if (typeof this._licenseInputValue !== "string") this._licenseInputValue = "";
    if (!this._actionState) this._actionState = {};
    if (!this._readingCache) this._readingCache = new Map();
    if (!this._lastHassStateVersion) this._lastHassStateVersion = 0;
    if (typeof this._invoiceGroupingSelectActive !== "boolean") this._invoiceGroupingSelectActive = false;

    const configuredGrouping = Object.prototype.hasOwnProperty.call(rawConfig, "invoice_grouping")
      ? this._normalizeInvoiceGrouping(rawConfig.invoice_grouping)
      : "";
    const storedGrouping = this._normalizeInvoiceGrouping(this._loadInvoiceGroupingPreference());
    if (!this._isValidInvoiceGrouping(this._invoiceGrouping)) {
      this._invoiceGrouping = configuredGrouping || storedGrouping || "location";
    }

    this._licenseInputEntityId = null;
    this._licenseApplyEntityId = null;
  }

  set hass(hass) {
    this._hass = hass;
    if ((this._isReadingInputActive() && !this._hasPendingReadingAction()) || this._isInvoiceGroupingSelectActive()) {
      return;
    }
    this._render();
  }

  _isInvoiceGroupingSelectActive() {
    if (this._invoiceGroupingSelectActive) return true;
    const activeElement = this.content?.querySelector?.(".invoice-grouping-select");
    return !!activeElement && activeElement === this.ownerDocument?.activeElement;
  }

  getCardSize() {
    return this._config.show_license ? 12 : 10;
  }

  _findEntityId() {
    if (this._config.entity && this._hass.states[this._config.entity]) {
      return this._config.entity;
    }

    if (this._hass.states["sensor.administrare_integrare_facturi_utilitati"]) {
      return "sensor.administrare_integrare_facturi_utilitati";
    }

    const candidates = Object.keys(this._hass.states).filter((entityId) => {
      if (!entityId.startsWith("sensor.")) return false;
      const stateObj = this._hass.states[entityId];
      const attrs = stateObj?.attributes || {};
      return Array.isArray(attrs.locatii);
    });

    return candidates[0] || null;
  }

  _normalizeStatus(value) {
    const text = String(value ?? "").trim().toLowerCase();
    if (!text) return "unknown";
    if (["paid", "platita", "plătită", "platit", "achitata", "achitată"].includes(text)) return "paid";
    if (["unpaid", "neplatita", "neplătită", "neplatit", "restanta", "restanță", "de_plata", "de plată"].includes(text)) return "unpaid";
    if (["credit", "prosumator"].includes(text)) return "credit";
    return text;
  }

  _statusLabel(status) {
    if (status === "paid") return "Plătită";
    if (status === "unpaid") return "Neplătită";
    if (status === "credit") return "Credit";
    return "Necunoscut";
  }

  _providerEffectiveStatus(provider) {
    if (provider?.manual_status_override === true) {
      return "paid";
    }
    return this._normalizeStatus(provider?.status || provider?.payment_status || provider?.status_raw);
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

  _providerInvoiceAmount(provider) {
    return provider?.amount ?? provider?.suma ?? provider?.valoare ?? provider?.total ?? null;
  }

  _isRerVestProvider(provider) {
    const key = String(provider?.furnizor || provider?.provider || provider?.provider_key || provider?.platform || "").toLowerCase();
    const label = String(provider?.furnizor_label || provider?.supplier || provider?.name || "").toLowerCase();
    return key === "rervest" || key === "rer_vest" || label.includes("rer vest");
  }

  _providerUnpaidTotalValue(provider) {
    if (this._providerEffectiveStatus(provider) !== "unpaid") return 0;
    const total = this._toNumber(provider?.unpaid_total ?? provider?.unpaid_amount);
    if (total > 0) return total;
    return this._toNumber(this._providerInvoiceAmount(provider));
  }

  _providerDisplayAmount(provider) {
    const invoiceAmount = this._toNumber(this._providerInvoiceAmount(provider));
    const unpaidTotal = this._providerUnpaidTotalValue(provider);

    // Dacă totalul de plată este mai mare decât ultima factură, afișăm totalul restant.
    if (this._isRerVestProvider(provider) && this._providerEffectiveStatus(provider) === "unpaid" && unpaidTotal > invoiceAmount) {
      return unpaidTotal;
    }

    return this._providerInvoiceAmount(provider) ?? provider?.unpaid_amount ?? null;
  }

  _formatDate(value) {
    if (!value || value === "-") return "—";
    const text = String(value).trim();
    if (!text) return "—";
    if (/^\d{2}\.\d{2}\.\d{4}$/.test(text)) return text;
    const parsed = new Date(text);
    if (!Number.isNaN(parsed.getTime())) {
      try {
        return new Intl.DateTimeFormat("ro-RO").format(parsed);
      } catch (_err) {
        return text;
      }
    }
    return text;
  }

  _formatDateTime(value) {
    if (!value || value === "-") return "—";
    const parsed = new Date(value);
    if (!Number.isNaN(parsed.getTime())) {
      try {
        return new Intl.DateTimeFormat("ro-RO", {
          dateStyle: "short",
          timeStyle: "short",
        }).format(parsed);
      } catch (_err) {
        return this._formatDate(value);
      }
    }
    return this._formatDate(value);
  }

  _toNumber(value) {
    if (typeof value === "number") return Number.isFinite(value) ? value : 0;
    if (typeof value === "string") {
      const normalized = value.replace(/\s/g, "").replace(",", ".");
      const parsed = Number(normalized);
      return Number.isFinite(parsed) ? parsed : 0;
    }
    return 0;
  }

  _readNumericState(entityId) {
    const state = entityId ? this._hass?.states?.[entityId]?.state : null;
    if (state === null || state === undefined || ["unknown", "unavailable"].includes(String(state))) {
      return null;
    }
    const value = this._toNumber(state);
    return Number.isFinite(value) ? value : null;
  }

  async _waitForNumberState(entityId, expectedValue, timeoutMs = 15000) {
    const expected = Number(expectedValue);
    const startedAt = Date.now();

    while (Date.now() - startedAt <= timeoutMs) {
      const current = this._readNumericState(entityId);
      if (current !== null && Math.abs(current - expected) < 0.000001) {
        return true;
      }
      await this._sleep(300);
    }

    return false;
  }

  _formatMoney(value, currency = "RON") {
    if (value === null || value === undefined || value === "") return "—";
    const amount = this._toNumber(value);
    try {
      return new Intl.NumberFormat("ro-RO", {
        style: "currency",
        currency,
        maximumFractionDigits: 2,
      }).format(amount);
    } catch (_err) {
      return `${amount.toFixed(2)} ${currency}`;
    }
  }

  _escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  _escapeAttr(value) {
    return this._escapeHtml(value);
  }

  _normalizeText(value) {
    return String(value ?? "")
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, " ")
      .trim();
  }

  _makeKey(...parts) {
    return parts.map((part) => String(part ?? "")).join("__");
  }

  _parseDateLike(value) {
    if (!value) return null;
    const text = String(value).trim();
    if (!text || text === "-") return null;

    if (/^\d{4}-\d{2}-\d{2}$/.test(text)) {
      return new Date(`${text}T00:00:00`);
    }
    if (/^\d{4}-\d{2}-\d{2}T/.test(text)) {
      const parsed = new Date(text);
      if (Number.isNaN(parsed.getTime())) return null;
      return new Date(parsed.getFullYear(), parsed.getMonth(), parsed.getDate());
    }
    if (/^\d{2}[./-]\d{2}[./-]\d{4}$/.test(text)) {
      const [dd, mm, yyyy] = text.split(/[./-]/);
      return new Date(`${yyyy}-${mm}-${dd}T00:00:00`);
    }

    const parsed = new Date(text);
    if (Number.isNaN(parsed.getTime())) return null;
    return new Date(parsed.getFullYear(), parsed.getMonth(), parsed.getDate());
  }

  _daysPastDue(value) {
    const date = this._parseDateLike(value);
    if (!date) return null;

    const diffMs = this._todayDate().getTime() - date.getTime();
    const diffDays = Math.floor(diffMs / 86400000);
    return diffDays > 0 ? diffDays : 0;
  }

  _daysUntilDue(value) {
    const date = this._parseDateLike(value);
    if (!date) return null;

    const diffMs = date.getTime() - this._todayDate().getTime();
    return Math.ceil(diffMs / 86400000);
  }

  _buildDueDateMeta(provider, status) {
    const rawDueDate = provider?.due_date || provider?.data_scadenta || provider?.scadenta || provider?.dueDate;
    const formattedDueDate = this._formatDate(rawDueDate);
    if (!rawDueDate || formattedDueDate === "—") return "";

    const daysPastDue = this._daysPastDue(rawDueDate);
    const daysUntilDue = this._daysUntilDue(rawDueDate);
    const isOverdue = status === "unpaid" && Number.isFinite(daysPastDue) && daysPastDue > 0;
    const isDueSoon = status === "unpaid" && !isOverdue && Number.isFinite(daysUntilDue) && daysUntilDue >= 0 && daysUntilDue <= 5;
    const overdueText = daysPastDue === 1 ? "depășită cu 1 zi" : `depășită cu ${daysPastDue} zile`;
    const dueClass = isOverdue ? "row-due-overdue" : (isDueSoon ? "row-due-soon" : "");

    return `
      <div class="row-due ${dueClass}">
        Scadentă: ${this._escapeHtml(formattedDueDate)}${isOverdue ? ` • ${this._escapeHtml(overdueText)}` : ""}
      </div>
    `;
  }

  _todayDate() {
    const now = new Date();
    return new Date(now.getFullYear(), now.getMonth(), now.getDate());
  }

  _filterLocations(locations) {
    const onlyUnpaid = !!this._config.only_unpaid;
    const showPaid = this._config.show_paid !== false;

    return locations
      .map((location) => {
        const providers = Array.isArray(location.furnizori) ? location.furnizori : [];
        const filteredProviders = providers.filter((provider) => {
          const status = this._providerEffectiveStatus(provider);
          if (onlyUnpaid) return status === "unpaid";
          if (!showPaid && status === "paid") return false;
          return true;
        });

        if (!filteredProviders.length) return null;
        return { ...location, furnizori: filteredProviders };
      })
      .filter(Boolean);
  }

  _invoiceGroupingStorageKey() {
    return "utilitati_romania_invoice_grouping";
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

  _normalizeInvoiceGrouping(value) {
    const text = String(value ?? "").trim().toLowerCase();
    const aliases = {
      locatie: "location",
      locatii: "location",
      cont: "location",
      account: "location",
      due: "due_date",
      scadenta: "due_date",
      scadență: "due_date",
      data_scadenta: "due_date",
      plata: "status",
      status_plata: "status",
      urgent: "urgency",
      urgenta: "urgency",
      urgență: "urgency",
      valoare: "amount",
      suma: "amount",
      amount: "amount",
      furnizor: "provider",
      supplier: "provider",
    };
    return aliases[text] || text;
  }

  _isValidInvoiceGrouping(value) {
    const normalized = this._normalizeInvoiceGrouping(value);
    return this._invoiceGroupingOptions().some((option) => option.value === normalized);
  }

  _loadInvoiceGroupingPreference() {
    try {
      return window.localStorage?.getItem(this._invoiceGroupingStorageKey()) || "";
    } catch (_err) {
      return "";
    }
  }

  _saveInvoiceGroupingPreference(value) {
    try {
      window.localStorage?.setItem(this._invoiceGroupingStorageKey(), value);
    } catch (_err) {
      // Preferința de afișare este doar locală în card; dacă browserul nu permite salvarea, folosim valoarea din sesiunea curentă.
    }
  }

  _getInvoiceGrouping() {
    if (!this._isValidInvoiceGrouping(this._invoiceGrouping)) {
      this._invoiceGrouping = "location";
    }
    return this._invoiceGrouping;
  }

  _setInvoiceGrouping(value) {
    const normalized = this._normalizeInvoiceGrouping(value);
    this._invoiceGrouping = this._isValidInvoiceGrouping(normalized) ? normalized : "location";
    this._saveInvoiceGroupingPreference(this._invoiceGrouping);
  }

  _buildInvoiceGroupingSelector(grouping, invoiceCount) {
    const options = this._invoiceGroupingOptions()
      .map((option) => `
        <option value="${this._escapeAttr(option.value)}" ${option.value === grouping ? "selected" : ""}>
          ${this._escapeHtml(option.label)}
        </option>
      `)
      .join("");

    return `
      <div class="invoice-toolbar">
        <label class="invoice-grouping-label" for="ur-invoice-grouping">Afișare facturi</label>
        <select id="ur-invoice-grouping" class="invoice-grouping-select">
          ${options}
        </select>
        <div class="invoice-grouping-hint">${this._escapeHtml(invoiceCount)} ${invoiceCount === 1 ? "factură" : "facturi"}</div>
      </div>
    `;
  }

  _collectInvoiceEntries(locations) {
    const entries = [];
    for (const location of locations || []) {
      const providers = Array.isArray(location.furnizori) ? location.furnizori : [];
      providers.forEach((provider, index) => {
        entries.push({
          location,
          provider,
          index,
          status: this._providerEffectiveStatus(provider),
          supplier: provider?.furnizor_label || provider?.furnizor || "Furnizor",
          dueDate: provider?.due_date || provider?.data_scadenta || provider?.scadenta || provider?.dueDate || "",
          amount: this._toNumber(this._providerDisplayAmount(provider)),
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
    return {
      key: `status_${status}`,
      title: this._statusLabel(status),
      order: order[status] ?? 9,
    };
  }

  _invoiceProviderGroup(entry) {
    const supplier = entry?.supplier || "Furnizor";
    return {
      key: `provider_${this._normalizeText(supplier) || "furnizor"}`,
      title: supplier,
      order: 0,
    };
  }

  _invoiceUrgencyGroup(entry) {
    const daysPastDue = this._daysPastDue(entry?.dueDate);
    const daysUntilDue = this._daysUntilDue(entry?.dueDate);

    if (entry?.status === "unpaid" && Number.isFinite(daysPastDue) && daysPastDue > 0) {
      return { key: "urgency_overdue", title: "Depășite", order: 0 };
    }
    if (entry?.status === "unpaid" && Number.isFinite(daysUntilDue) && daysUntilDue >= 0 && daysUntilDue <= 5) {
      return { key: "urgency_soon", title: "Scadente în următoarele 5 zile", order: 1 };
    }
    if (entry?.status === "unpaid") {
      return { key: "urgency_unpaid", title: "Neplătite", order: 2 };
    }
    if (entry?.status === "paid") {
      return { key: "urgency_paid", title: "Plătite", order: 3 };
    }
    if (entry?.status === "credit") {
      return { key: "urgency_credit", title: "Credit", order: 4 };
    }
    return { key: "urgency_unknown", title: "Necunoscute", order: 5 };
  }

  _invoiceAmountGroup(entry) {
    const amount = this._toNumber(entry?.amount);
    if (!amount) return { key: "amount_none", title: "Fără valoare", order: 99 };
    if (amount >= 500) return { key: "amount_500", title: "Peste 500 lei", order: 0 };
    if (amount >= 200) return { key: "amount_200_499", title: "200–499 lei", order: 1 };
    if (amount >= 100) return { key: "amount_100_199", title: "100–199 lei", order: 2 };
    return { key: "amount_under_100", title: "Sub 100 lei", order: 3 };
  }

  _invoiceDueDateGroup(entry) {
    const date = this._parseDateLike(entry?.dueDate);
    if (!date || !Number.isFinite(date.getTime())) {
      return { key: "due_none", title: "Fără scadență", order: 50 };
    }

    const daysPastDue = this._daysPastDue(entry.dueDate);
    const daysUntilDue = this._daysUntilDue(entry.dueDate);

    if (entry.status === "unpaid" && Number.isFinite(daysPastDue) && daysPastDue > 0) {
      return { key: "due_overdue", title: "Depășite", order: 0 };
    }
    if (Number.isFinite(daysUntilDue) && daysUntilDue === 0) {
      return { key: "due_today", title: "Scadente astăzi", order: 1 };
    }
    if (Number.isFinite(daysUntilDue) && daysUntilDue > 0 && daysUntilDue <= 5) {
      return { key: "due_soon", title: "Următoarele 5 zile", order: 2 };
    }

    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const title = (() => {
      try {
        return new Intl.DateTimeFormat("ro-RO", { month: "long", year: "numeric" }).format(date);
      } catch (_err) {
        return `${month}.${year}`;
      }
    })();

    return {
      key: `due_${year}_${month}`,
      title: title.charAt(0).toUpperCase() + title.slice(1),
      order: 10 + year * 12 + date.getMonth(),
    };
  }

  _invoiceGroupForEntry(entry, grouping) {
    if (grouping === "status") return this._invoiceStatusGroup(entry);
    if (grouping === "provider") return this._invoiceProviderGroup(entry);
    if (grouping === "due_date") return this._invoiceDueDateGroup(entry);
    if (grouping === "urgency") return this._invoiceUrgencyGroup(entry);
    if (grouping === "amount") return this._invoiceAmountGroup(entry);
    return {
      key: entry?.location?.locatie_cheie || entry?.location?.eticheta_locatie || "locatie",
      title: entry?.location?.eticheta_locatie || entry?.location?.locatie_cheie || "Locație",
      order: 0,
    };
  }

  _buildInvoiceGroups(entries, grouping) {
    const groups = new Map();

    for (const entry of entries || []) {
      const info = this._invoiceGroupForEntry(entry, grouping);
      if (!groups.has(info.key)) {
        groups.set(info.key, {
          key: info.key,
          title: info.title,
          order: info.order,
          entries: [],
        });
      }
      groups.get(info.key).entries.push(entry);
    }

    return Array.from(groups.values())
      .map((group) => ({
        ...group,
        entries: group.entries.sort((a, b) => {
          if (grouping === "amount") {
            const amountDiff = this._toNumber(b.amount) - this._toNumber(a.amount);
            if (amountDiff !== 0) return amountDiff;
          }
          return this._compareInvoiceEntries(a, b);
        }),
      }))
      .sort((a, b) => {
        const orderDiff = (a.order ?? 0) - (b.order ?? 0);
        if (orderDiff !== 0) return orderDiff;
        return String(a.title || "").localeCompare(String(b.title || ""), "ro");
      });
  }

  _providerUnpaidCount(provider) {
    if (this._providerEffectiveStatus(provider) !== "unpaid") return 0;

    const providerKey = String(provider?.furnizor || provider?.furnizor_key || "").toLowerCase();
    const providerLabel = String(provider?.furnizor_label || "").toLowerCase();

    // La DIGI, fiecare rând afișat în card reprezintă factura curentă a unui
    // serviciu/loc de consum. Structura brută poate conține lista comună de
    // facturi neplătite ale contului, iar folosirea acelei liste dublează
    // numărul afișat în antet, deși valoarea totală este corectă.
    if (providerKey === "digi" || providerLabel === "digi") {
      return 1;
    }

    const explicitCount = this._toNumber(provider?.unpaid_count);
    if (Number.isFinite(explicitCount) && explicitCount >= 0) {
      return Math.round(explicitCount);
    }

    if (Array.isArray(provider?.unpaid_invoice_ids) && provider.unpaid_invoice_ids.length > 0) {
      return provider.unpaid_invoice_ids.length;
    }

    const invoiceAmount = this._toNumber(this._providerInvoiceAmount(provider));
    const unpaidTotal = this._providerUnpaidTotal(provider);
    if (this._isRerVestProvider(provider) && invoiceAmount > 0 && unpaidTotal > invoiceAmount) {
      return 2;
    }

    return 1;
  }

  _providerUnpaidTotal(provider) {
    return this._providerUnpaidTotalValue(provider);
  }

  _invoiceGroupSummary(entries) {
    const providers = (entries || []).map((entry) => entry.provider).filter(Boolean);
    const paid = providers.filter((provider) => this._providerEffectiveStatus(provider) === "paid").length;
    const unpaid = providers.reduce((sum, provider) => sum + this._providerUnpaidCount(provider), 0);
    const credit = providers.filter((provider) => this._providerEffectiveStatus(provider) === "credit").length;
    const totalUnpaid = providers.reduce((sum, provider) => sum + this._providerUnpaidTotal(provider), 0);

    const parts = [];
    parts.push(`${providers.length} ${providers.length === 1 ? "factură" : "facturi"}`);
    if (unpaid > 0) parts.push(`${unpaid} neplătite`);
    if (paid > 0) parts.push(`${paid} plătite`);
    if (credit > 0) parts.push(`${credit} credit`);
    if (totalUnpaid > 0) parts.push(`total neplătit ${this._formatMoney(totalUnpaid)}`);

    return parts.join(" • ");
  }

  _renderInvoicesByLocation(locations) {
    return (locations || [])
      .map((location) => {
        const openReading = this._getAnyOpenReadingForLocation(location);
        const hasUnpaid = (location.furnizori || []).some((p) => this._providerEffectiveStatus(p) === "unpaid");

        return `
          <div class="location ${hasUnpaid ? 'location-unpaid' : ''}">
            <div class="location-heading">
              <div class="location-title">${this._escapeHtml(location.eticheta_locatie || location.locatie_cheie || "Locație")}</div>
              ${openReading?.badge ? this._buildReadingBadge(openReading.badge) : ""}
            </div>
            <div class="location-meta">${this._escapeHtml(this._locationSummary(location))}</div>
            <div class="invoice-list">
              ${(location.furnizori || []).map((provider, index) => this._buildProviderRow(location, provider, index)).join("")}
            </div>
          </div>
        `;
      })
      .join("");
  }

  _renderInvoicesByGrouping(entries, grouping) {
    return this._buildInvoiceGroups(entries, grouping)
      .map((group) => {
        const hasUnpaid = group.entries.some((entry) => entry.status === "unpaid");
        const hasOpenReading = group.entries.some((entry) => this._getReadingData(entry.location, entry.provider).isOpen);

        return `
          <div class="location invoice-group ${hasUnpaid ? 'location-unpaid' : ''}">
            <div class="location-heading">
              <div class="location-title">${this._escapeHtml(group.title)}</div>
              ${hasOpenReading ? this._buildReadingBadge("Citire deschisă") : ""}
            </div>
            <div class="location-meta">${this._escapeHtml(this._invoiceGroupSummary(group.entries))}</div>
            <div class="invoice-list">
              ${group.entries.map((entry) => this._buildProviderRow(entry.location, entry.provider, entry.index)).join("")}
            </div>
          </div>
        `;
      })
      .join("");
  }

  _locationSummary(location) {
    const providers = Array.isArray(location.furnizori) ? location.furnizori : [];
    const paid = providers.filter((item) => this._providerEffectiveStatus(item) === "paid").length;
    const unpaid = providers.reduce((sum, item) => sum + this._providerUnpaidCount(item), 0);
    const credit = providers.filter((item) => this._providerEffectiveStatus(item) === "credit").length;
    const totalUnpaid = providers.reduce((sum, item) => sum + this._providerUnpaidTotal(item), 0);

    const parts = [];
    parts.push(`${providers.length} ${providers.length === 1 ? "factură" : "facturi"}`);
    if (paid > 0) parts.push(`${paid} plătite`);
    if (unpaid > 0) parts.push(`${unpaid} neplătite`);
    if (credit > 0) parts.push(`${credit} credit`);
    if (totalUnpaid > 0) parts.push(`total neplătit ${this._formatMoney(totalUnpaid)}`);

    return parts.join(" • ");
  }

  _buildSummary(attrs) {
    const total = this._toNumber(attrs.numar_facturi);
    const paid = this._toNumber(attrs.numar_platite);
    const unpaid = this._toNumber(attrs.numar_neplatite);
    const unknown = this._toNumber(attrs.numar_necunoscute ?? attrs.numar_status_necunoscut);
    const totalUnpaid = attrs.total_neplatit_formatat || this._formatMoney(attrs.total_neplatit, attrs.moneda || "RON");

    return `
      <div class="summary">
        <div><span class="summary-label">Facturi:</span> <span class="summary-value">${total}</span></div>
        <div><span class="summary-label">Plătite:</span> <span class="summary-value">${paid}</span></div>
        <div><span class="summary-label">Neplătite:</span> <span class="summary-value">${unpaid}</span></div>
        <div><span class="summary-label">Necunoscute:</span> <span class="summary-value">${unknown}</span></div>
        <div><span class="summary-label">Total neplătit:</span> <span class="summary-value">${this._escapeHtml(totalUnpaid)}</span></div>
      </div>
    `;
  }

  _rowKey(location, provider, index) {
    const loc = location.locatie_cheie || location.eticheta_locatie || "loc";
    const furn = provider.furnizor || provider.furnizor_label || "furnizor";
    const inv = provider.invoice_id || provider.invoice_title || index;
    return `${loc}__${furn}__${inv}__${index}`;
  }

  _isExpanded(key) {
    return !!this._expanded[key];
  }

  _providerCompactTitle(provider) {
    return provider.invoice_title || provider.invoice_id || "Ultima factură";
  }

  _entityFriendlyText(stateObj) {
    const friendly = stateObj?.attributes?.friendly_name || "";
    return this._normalizeText(`${stateObj?.entity_id || ""} ${friendly}`);
  }

  _textMatchesAny(text, terms) {
    const hay = this._normalizeText(text || "");
    return (terms || []).some((term) => term && hay.includes(term));
  }

  _readingTerms(location, provider) {
    const values = [
      location?.eticheta_locatie,
      provider?.nume_cont,
      provider?.adresa_originala,
      provider?.invoice_title,
    ];

    const normalized = values
      .map((value) => this._normalizeText(value))
      .filter(Boolean);

    const extra = [];
    for (const value of normalized) {
      const noNumbers = value.replace(/\b\d+\b/g, " ").replace(/\s+/g, " ").trim();
      if (noNumbers && noNumbers !== value) extra.push(noNumbers);
    }

    return Array.from(new Set([...normalized, ...extra])).filter((value) => value.length >= 3);
  }

  _findReadingSensor(location, provider) {
    const providerKey = String(provider?.furnizor || "").trim().toLowerCase();
    const targetIdCont = String(provider?.id_cont ?? "").trim();
    const targetIdContract = String(provider?.id_contract ?? "").trim();
    const terms = this._readingTerms(location, provider);
    const normalizedProvider = providerKey.replace(/_/g, " ");

    if (!providerKey || !["hidroelectrica", "eon", "myelectrica", "apa_canal", "apa_brasov", "apa_oradea", "hidro_prahova"].includes(providerKey)) {
      return null;
    }

    const candidates = Object.values(this._hass?.states || {}).filter((stateObj) => {
      if (!stateObj?.entity_id?.startsWith("sensor.")) return false;
      const entityId = stateObj.entity_id;
      const attrs = stateObj.attributes || {};
      const text = this._entityFriendlyText(stateObj);

      const looksLikeReadingSensor = !!(
        entityId.includes("citire_permisa") ||
        text.includes("citire permisa") ||
        attrs.inceput_perioada || attrs.sfarsit_perioada || attrs["Perioadă start"] || attrs["Perioadă sfârșit"]
      );

      if (!looksLikeReadingSensor) return false;

      const providerMatches = entityId.includes(providerKey) || text.includes(normalizedProvider);
      return providerMatches;
    });

    let best = null;
    let bestScore = -1;

    for (const stateObj of candidates) {
      const entityId = stateObj.entity_id;
      const attrs = stateObj.attributes || {};
      const text = this._entityFriendlyText(stateObj);
      let score = 0;

      score += 50; // provider match is already mandatory

      const attrIdCont = String(attrs.id_cont ?? "").trim();
      if (targetIdCont && attrIdCont) {
        if (attrIdCont === targetIdCont) {
          score += 120;
        } else {
          continue;
        }
      }

      const attrContract = String(attrs.id_contract ?? attrs.cod_contract ?? "").trim();
      if (targetIdContract && attrContract) {
        if (attrContract === targetIdContract) {
          score += 100;
        } else {
          continue;
        }
      }

      const attrAddress = this._normalizeText(attrs.adresa || attrs["Adresă"] || attrs.apartament || attrs.nume_cont || "");
      if (attrAddress && this._textMatchesAny(attrAddress, terms)) score += 70;

      if (this._textMatchesAny(text, terms)) score += 80;

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
    const startRaw =
      attrs.inceput_perioada ||
      attrs["inceput_perioada"] ||
      attrs["Perioadă start"] ||
      attrs.StartDatePAC ||
      attrs.start_date ||
      null;
    const endRaw =
      attrs.sfarsit_perioada ||
      attrs["sfarsit_perioada"] ||
      attrs["Perioadă sfârșit"] ||
      attrs.EndDatePAC ||
      attrs.end_date ||
      null;

    const startDate = this._parseDateLike(startRaw);
    const endDate = this._parseDateLike(endRaw);
    const today = this._todayDate();

    let openByRange = false;
    if (startDate && endDate) {
      openByRange = today >= new Date(startDate.getFullYear(), startDate.getMonth(), startDate.getDate()) && today <= new Date(endDate.getFullYear(), endDate.getMonth(), endDate.getDate());
    }

    const stateText = this._normalizeText(sensorState.state);
    const truthyState = ["da", "yes", "true", "on", "activ", "disponibil", "permisa", "permis"].includes(stateText);

    return {
      isOpen: openByRange || truthyState,
      start: startRaw || null,
      end: endRaw || null,
    };
  }

  _deriveControlsFromReadingSensor(location, provider, readingSensor) {
    if (!readingSensor) return [];

    const providerKey = String(provider?.furnizor || "").toLowerCase();
    const sensorEntityId = readingSensor.entity_id || "";
    const sensorObject = sensorEntityId.replace(/^sensor\./, "");
    const states = this._hass?.states || {};
    const readingText = this._entityFriendlyText(readingSensor);
    const terms = this._readingTerms(location, provider);

    const controls = [];

    if (providerKey === "hidroelectrica") {
      const base = sensorObject.replace(/_citire_permisa$/, "");
      const numberEntityId = `number.${base}_index_energie_electrica`;
      const buttonEntityId = `button.${base}_trimite_index`;
      const currentEntityId = `sensor.${base}_index_energie_electrica`;
      controls.push({
        key: `${providerKey}_${provider.id_cont || base}`,
        label: "Index de transmis",
        numberEntityId,
        buttonEntityId,
        currentEntityId,
      });
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
        return (
          entityId.includes("hidro") ||
          entityId.includes("hidroelectrica") ||
          entityId.includes("myelectrica") ||
          entityId.includes("apa_canal") ||
          entityId.includes("apa_brasov") || entityId.includes("apa_oradea") || entityId.includes("hidro_prahova") ||
          entityId.includes("apacanal") ||
          entityId.includes("ebloc") ||
          text.includes("hidro") ||
          text.includes("hidroelectrica") ||
          text.includes("myelectrica") ||
          text.includes("apa canal") ||
          text.includes("apa brasov") ||
          text.includes("apă brașov") ||
          text.includes("ebloc")
        );
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
          if (score > bestScore) {
            best = stateObj;
            bestScore = score;
          }
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

      if (numberEntity && buttonEntity) {
        controls.push({
          key: `${providerKey}_${provider.id_cont || base}`,
          providerKey,
          label: "Index de transmis",
          numberEntityId: numberEntity.entity_id,
          buttonEntityId: buttonEntity.entity_id,
          currentEntityId: currentEntity?.entity_id || null,
        });
      }
      return controls;
    }

    if (providerKey === "myelectrica") {
      const parts = sensorObject.split("_");
      const slug = parts.slice(3, -1).join("_");
      const numberEntityId = `number.utilitati_romania_myelectrica_${slug}_index_contor`;
      const numberEntity = states[numberEntityId] || null;
      let currentEntity = Object.values(states).find((stateObj) => {
        if (!stateObj.entity_id.startsWith("sensor.")) return false;
        const attrs = stateObj.attributes || {};
        return String(attrs.id_cont ?? "") === String(provider.id_cont ?? "") && (stateObj.entity_id.includes("index_contor") || this._entityFriendlyText(stateObj).includes("index contor"));
      });
      if (!currentEntity) {
        currentEntity = Object.values(states).find((stateObj) => {
          if (!stateObj.entity_id.startsWith("sensor.")) return false;
          const text = this._entityFriendlyText(stateObj);
          return this._textMatchesAny(text, terms) && (text.includes("index contor") || text.includes("index"));
        });
      }
      const buttonEntity = Object.values(states).find((stateObj) => {
        if (!stateObj.entity_id.startsWith("button.")) return false;
        const text = this._entityFriendlyText(stateObj);
        return text.includes("trimite index") && this._textMatchesAny(text, terms);
      });
      if (numberEntity && buttonEntity) {
        controls.push({
          key: `${providerKey}_${provider.id_cont || slug}`,
          label: "Index de transmis",
          numberEntityId,
          buttonEntityId: buttonEntity.entity_id,
          currentEntityId: currentEntity?.entity_id || null,
        });
      }
      return controls;
    }

    if (providerKey === "apa_canal" || providerKey === "apa_brasov" || providerKey === "apa_oradea" || providerKey === "hidro_prahova") {
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

      const numberFromAttr = attrs.number_entity_id && states[attrs.number_entity_id]
        ? states[attrs.number_entity_id]
        : null;
      const buttonFromAttr = attrs.button_entity_id && states[attrs.button_entity_id]
        ? states[attrs.button_entity_id]
        : null;

      const numberEntity = numberFromAttr || states[expectedNumberEntityId] || Object.values(states).find((stateObj) => {
        if (!stateObj.entity_id.startsWith("number.")) return false;
        const text = this._entityFriendlyText(stateObj);
        return text.includes("index de transmis") && matchesApaCanalContext(stateObj);
      });
      const buttonEntity = buttonFromAttr || states[expectedButtonEntityId] || Object.values(states).find((stateObj) => {
        if (!stateObj.entity_id.startsWith("button.")) return false;
        const text = this._entityFriendlyText(stateObj);
        return text.includes("trimite index") && matchesApaCanalContext(stateObj);
      });
      const currentEntity = states[currentEntityId] || Object.values(states).find((stateObj) => {
        if (!stateObj.entity_id.startsWith("sensor.")) return false;
        const text = this._entityFriendlyText(stateObj);
        const stateAttrs = stateObj.attributes || {};
        const idCont = String(stateAttrs.id_cont ?? "").trim();
        const idContract = String(stateAttrs.id_contract ?? "").trim();
        const sameContext = (sensorIdCont && idCont && idCont === sensorIdCont) || (sensorIdContract && idContract && idContract === sensorIdContract);
        return (text.includes("ultimul index") || text.includes("index")) && (sameContext || this._textMatchesAny(text, terms) || stateObj.entity_id.includes(base));
      });

      if (numberEntity && buttonEntity) {
        controls.push({
          key: `${providerKey}_${provider.id_cont || sensorIdCont || base}`,
          label: "Index de transmis",
          numberEntityId: numberEntity.entity_id,
          buttonEntityId: buttonEntity.entity_id,
          currentEntityId: currentEntity?.entity_id || null,
        });
      }
      return controls;
    }

    return [];
  }

  _getReadingData(location, provider) {
    const cacheKey = this._makeKey(location.locatie_cheie, provider.furnizor, provider.id_cont, provider.id_contract);
    if (this._readingCache.has(cacheKey)) return this._readingCache.get(cacheKey);

    const providerKey = String(provider?.furnizor || "").trim().toLowerCase();
    if (providerKey === "ebloc" && provider?.reading_available) {
      const isOpen = provider.reading_is_open === true || this._normalizeText(provider.reading_is_open) === "da";
      const days = Number(provider.reading_days_until);
      let badge = null;

      if (isOpen) {
        badge = "Citire deschisă";
      } else if (Number.isFinite(days)) {
        if (days === 0) badge = "Citire deschisă";
        else if (days === 1) badge = "Citire mâine";
        else if (days > 1) badge = `Citire în ${days} zile`;
      }

      const result = {
        available: true,
        isOpen,
        controls: [],
        start: null,
        end: null,
        period: provider.reading_period || null,
        daysUntil: Number.isFinite(days) ? days : null,
        badge,
      };

      this._readingCache.set(cacheKey, result);
      return result;
    }

    const readingSensor = this._findReadingSensor(location, provider);
    if (!readingSensor) {
      const empty = { available: false, isOpen: false, controls: [], start: null, end: null, badge: null };
      this._readingCache.set(cacheKey, empty);
      return empty;
    }

    const windowInfo = this._extractWindowInfo(readingSensor);
    const controls = this._deriveControlsFromReadingSensor(location, provider, readingSensor).map((control) => {
      const numberState = control.numberEntityId ? this._hass.states[control.numberEntityId] : null;
      const currentState = control.currentEntityId ? this._hass.states[control.currentEntityId] : null;
      return {
        ...control,
        numberState,
        currentState,
        unit: numberState?.attributes?.unit_of_measurement || currentState?.attributes?.unit_of_measurement || "",
        currentValue: currentState ? currentState.state : null,
      };
    });

    if (!controls.length) {
      const empty = { available: false, isOpen: false, controls: [], start: null, end: null, badge: null };
      this._readingCache.set(cacheKey, empty);
      return empty;
    }

    const result = {
      available: true,
      isOpen: !!windowInfo.isOpen,
      start: windowInfo.start,
      end: windowInfo.end,
      badge: windowInfo.isOpen ? "Perioadă citire activă" : null,
      readingSensorEntityId: readingSensor.entity_id,
      controls,
    };

    this._readingCache.set(cacheKey, result);
    return result;
  }

  _getAnyOpenReadingForLocation(location) {
    const providers = Array.isArray(location.furnizori) ? location.furnizori : [];
    for (const provider of providers) {
      const data = this._getReadingData(location, provider);
      if (data.isOpen) return data;
    }
    return null;
  }

  _buildReadingBadge(text) {
    return `<span class="reading-badge">${this._escapeHtml(text)}</span>`;
  }

  _actionStateKey(type, entityId) {
    return `${type}__${entityId}`;
  }

  _isReadingInputActive() {
    const active = this?.content?.getRootNode?.().activeElement || document.activeElement;
    return !!(active && active.classList && active.classList.contains("reading-input"));
  }

  _hasPendingReadingAction() {
    return Object.entries(this._actionState || {}).some(([key, value]) => {
      return key.startsWith("reading__") && value && value.status === "sending";
    });
  }

  _getBackendReadingHistoryEntry(control) {
    const attrs = control?.currentState?.attributes || {};
    const value = attrs.ultima_citire_transmisa;
    const timestamp = attrs.ultima_citire_transmisa_la;
    if (value === undefined || value === null || value === "" || !timestamp) {
      return null;
    }
    return {
      value,
      unit: control?.unit || control?.currentState?.attributes?.unit_of_measurement || "",
      timestamp,
    };
  }

  _getActionState(type, entityId) {
    return this._actionState[this._actionStateKey(type, entityId)] || { status: "idle", message: "" };
  }

  _setActionState(type, entityId, patch) {
    const key = this._actionStateKey(type, entityId);
    this._actionState[key] = {
      ...(this._actionState[key] || { status: "idle", message: "" }),
      ...patch,
    };
  }

  async _sleep(ms) {
    await new Promise((resolve) => window.setTimeout(resolve, ms));
  }

  async _refreshEntities(entityIds = []) {
    const ids = Array.from(new Set((entityIds || []).filter(Boolean)));
    if (!ids.length) return;
    try {
      await this._hass.callService("homeassistant", "update_entity", { entity_id: ids });
    } catch (_err) {
      // ignorăm erorile de refresh punctual; cardul se va reîmprospăta oricum la următorul update HA
    }
  }

  _buildReadingControls(location, provider) {
    const data = this._getReadingData(location, provider);
    if (!data.isOpen || !data.controls.length) return "";

    const periodText = data.start && data.end
      ? `Perioadă activă: ${this._formatDate(data.start)} – ${this._formatDate(data.end)}`
      : (data.end ? `Poți transmite până în ${this._formatDate(data.end)}` : "Perioada de transmitere este activă.");

    const controlsHtml = data.controls
      .map((control) => {
        const action = this._getActionState("reading", control.buttonEntityId);
        const disabled = action.status === "sending";
        const numberValue = control.numberState?.state && !["unknown", "unavailable"].includes(control.numberState.state)
          ? control.numberState.state
          : "";
        const currentText = control.currentValue && !["unknown", "unavailable"].includes(control.currentValue)
          ? `${control.currentValue}${control.unit ? ` ${control.unit}` : ""}`
          : "—";
        const lastSent = this._getBackendReadingHistoryEntry(control);
        const lastSentHtml = lastSent
          ? `<div class="reading-last-sent">Ultima transmitere: <strong>${this._escapeHtml(String(lastSent.value))}${this._escapeHtml(lastSent.unit ? ` ${lastSent.unit}` : "")}</strong> — ${this._escapeHtml(this._formatDateTime(lastSent.timestamp))}</div>`
          : "";

        return `
          <div
            class="reading-control"
            data-number-entity="${this._escapeAttr(control.numberEntityId || "")}"
            data-button-entity="${this._escapeAttr(control.buttonEntityId || "")}"
            data-provider="${this._escapeAttr(String(provider?.furnizor || ""))}"
            data-entry-id="${this._escapeAttr(String(provider?.entry_id || ""))}"
            data-id-cont="${this._escapeAttr(String(provider?.id_cont || ""))}"
            data-id-contract="${this._escapeAttr(String(provider?.id_contract || ""))}"
            data-control-label="${this._escapeAttr(String(control.label || ""))}"
            data-current-value="${this._escapeAttr(String(control.currentValue ?? ""))}"
            data-current-entity="${this._escapeAttr(control.currentEntityId || "")}"
            data-unit="${this._escapeAttr(String(control.unit || ""))}"
          >
            <div class="reading-control-header">
              <div class="reading-control-title">${this._escapeHtml(control.label || "Index")}</div>
              <div class="reading-control-current">Index curent: ${this._escapeHtml(currentText)}</div>
            </div>
            <div class="reading-control-editor">
              <input
                class="reading-input"
                type="number"
                step="any"
                inputmode="decimal"
                value="${this._escapeAttr(numberValue)}"
                placeholder="Introduceți indexul"
                ${disabled ? "disabled" : ""}
              />
              <button class="reading-submit-btn" ${disabled ? "disabled" : ""}>
                ${disabled ? "Se trimite..." : "Trimite index"}
              </button>
            </div>
            ${lastSentHtml}
            ${
              action.status === "success"
                ? `<div class="inline-status status-success">${this._escapeHtml(action.message || "Index trimis cu succes.")}</div>`
                : action.status === "error"
                  ? `<div class="inline-status status-error">${this._escapeHtml(action.message || "Transmiterea a eșuat.")}</div>`
                  : ""
            }
          </div>
        `;
      })
      .join("");

    return `
      <div class="reading-wrap">
        <div class="reading-title">Transmitere index</div>
        <div class="reading-period">${this._escapeHtml(periodText)}</div>
        ${controlsHtml}
      </div>
    `;
  }

  _getProviderRefreshEntityId(provider) {
    const explicitEntityId = String(provider?.refresh_button_entity_id || "").trim();
    if (explicitEntityId) return explicitEntityId;
    return "";
  }

_buildProviderRefreshButton(provider) {
  const refreshEntityId = this._getProviderRefreshEntityId(provider);
  if (!refreshEntityId || provider?.can_refresh === false) return "";

  const action = this._getActionState("refresh", refreshEntityId);
  const isSending = action.status === "sending";

  return `
    <button
      class="refresh-btn icon-only"
      data-refresh-entity="${this._escapeAttr(refreshEntityId)}"
      title="${isSending ? "Se actualizează..." : "Actualizează"}"
      aria-label="${isSending ? "Se actualizează..." : "Actualizează"}"
      ${isSending ? "disabled" : ""}
    >
      ${isSending ? "⟳" : "↻"}
    </button>
  `;
}

  _buildProviderRefreshStatus(provider) {
    const refreshEntityId = this._getProviderRefreshEntityId(provider);
    if (!refreshEntityId) return "";

    const action = this._getActionState("refresh", refreshEntityId);
    if (action.status === "success") {
      return `<div class="inline-status status-success">${this._escapeHtml(action.message || "Datele furnizorului au fost actualizate.")}</div>`;
    }
    if (action.status === "error") {
      return `<div class="inline-status status-error">${this._escapeHtml(action.message || "Actualizarea furnizorului a eșuat.")}</div>`;
    }
    return "";
  }

  _buildManualInvoiceAction(location, provider, index) {
    const key = this._rowKey(location, provider, index);
    const action = this._getActionState("manual_status", key);
    const isManual = provider?.manual_status_override === true;
    const isPaidReal = provider.status === "paid" && !isManual;
    if (isPaidReal) {
      return "";
    }
    const isBusy = action.status === "sending";
    const label = isManual ? "Anulează marcarea" : "Marchează plătită";

    const payload = {
      entry_id: provider?.entry_id || "",
      provider: provider?.furnizor || "",
      id_cont: provider?.id_cont || "",
      invoice_id: provider?.invoice_id || "",
      invoice_title: provider?.invoice_title || "",
      issue_date: provider?.issue_date || provider?.data_emitere || "",
      amount: provider?.amount ?? "",
      currency: provider?.currency || "RON",
      status: isManual ? "clear" : "paid",
    };

    return `
      <button
        class="provider-app-btn manual-status-btn"
        data-key="${this._escapeAttr(key)}"
        data-payload='${this._escapeAttr(JSON.stringify(payload))}'
        ${isBusy ? "disabled" : ""}
      >
        ${isBusy ? "Se salvează..." : this._escapeHtml(label)}
      </button>
    `;
  }

  _buildManualInvoiceStatus(location, provider, index) {
    const key = this._rowKey(location, provider, index);
    const action = this._getActionState("manual_status", key);
    if (action.status === "success") {
      return `<div class="inline-status status-success">${this._escapeHtml(action.message || "Statusul facturii a fost actualizat.")}</div>`;
    }
    if (action.status === "error") {
      return `<div class="inline-status status-error">${this._escapeHtml(action.message || "Actualizarea statusului a eșuat.")}</div>`;
    }
    return "";
  }

  _buildProviderReadingDetails(readingData) {
    if (!readingData || !readingData.available) return "";

    const rows = [];

    if (readingData.period) {
      rows.push(`<div><span class="detail-label">Perioadă citire:</span> <span class="detail-value">${this._escapeHtml(readingData.period)}</span></div>`);
    }

    if (readingData.daysUntil !== null && readingData.daysUntil !== undefined) {
      const value = Number(readingData.daysUntil);
      let text = "—";

      if (Number.isFinite(value)) {
        if (value === 0) text = "deschisă acum";
        else if (value === 1) text = "mâine";
        else text = `${value} zile`;
      }

      rows.push(`<div><span class="detail-label">Citire index:</span> <span class="detail-value">${this._escapeHtml(text)}</span></div>`);
    }

    return rows.join("");
  }

  _buildProviderRow(location, provider, index) {
    const supplier = provider.furnizor_label || provider.furnizor || "Furnizor";
    const title = this._providerCompactTitle(provider);
    const amountFormatted = this._formatMoney(this._providerDisplayAmount(provider), provider.currency || "RON");
    const status = this._providerEffectiveStatus(provider);
    const statusLabel = this._statusLabel(status);
    const issueDate = this._formatDate(provider.issue_date || provider.data_emitere);
    const dueDate = this._formatDate(provider.due_date || provider.data_scadenta || provider.scadenta || provider.dueDate);
    const utilityType = this._providerUtilityType(provider);
    const tipServiciu = utilityType || provider.tip_serviciu || "—";
    const numeCont = provider.nume_cont || "—";
    const readingData = this._getReadingData(location, provider);
    const dueDateMeta = this._buildDueDateMeta(provider, status);

    const key = this._rowKey(location, provider, index);
    const expanded = this._isExpanded(key);

    let statusClass = "status-unknown";
    if (status === "paid") statusClass = "status-paid";
    else if (status === "unpaid") statusClass = "status-unpaid";
    else if (status === "credit") statusClass = "status-credit";
    const isUnpaid = status === "unpaid";

    return `
      <div class="invoice-row-wrap ${isUnpaid ? 'invoice-unpaid' : ''}">
        <div class="invoice-row">
          <div class="row-main">
            <div class="row-supplier">
              ${this._escapeHtml(supplier)}
              ${isUnpaid ? '<span class="badge-unpaid">DE PLATĂ</span>' : ''}
              ${readingData.isOpen ? this._buildReadingBadge("Citire deschisă") : ""}
            </div>
            <div class="row-title">${this._escapeHtml(title)}</div>
            ${utilityType ? `<div class="row-utility">${this._escapeHtml(utilityType)}</div>` : ""}
          </div>

          <div class="row-amount-block">
            <div class="row-amount">${this._escapeHtml(amountFormatted)}</div>
            ${dueDateMeta}
          </div>

          <div class="row-actions">
            ${this._buildProviderRefreshButton(provider)}
            <button class="details-btn" data-key="${this._escapeAttr(key)}">
              ${expanded ? "Ascunde" : "Detalii"}
            </button>
          </div>
        </div>

        ${
          expanded
            ? `
              <div class="invoice-details">
                <div><span class="detail-label">Status:</span> <span class="detail-value ${statusClass}">${this._escapeHtml(statusLabel)}</span></div>
                ${provider.manual_status_override ? `<div><span class="detail-label">Override local:</span> <span class="detail-value status-paid">${this._escapeHtml(provider.manual_status_label || "Marcată manual ca plătită")}</span></div>` : ""}
                <div><span class="detail-label">Data emiterii:</span> <span class="detail-value">${this._escapeHtml(issueDate)}</span></div>
                <div><span class="detail-label">Data scadenței:</span> <span class="detail-value">${this._escapeHtml(dueDate)}</span></div>
                <div><span class="detail-label">Serviciu:</span> <span class="detail-value">${this._escapeHtml(tipServiciu)}</span></div>
                <div><span class="detail-label">Cont:</span> <span class="detail-value">${this._escapeHtml(numeCont)}</span></div>
                ${this._buildProviderReadingDetails(readingData)}
                <div class="detail-actions">
                  ${provider.pdf_url ? `<button class="pdf-btn" data-url="${this._escapeAttr(provider.pdf_url)}">Deschide PDF</button>` : ""}
                  ${this._buildProviderOpenAppButton(provider)}
                  ${this._buildManualInvoiceAction(location, provider, index)}
                </div>
                ${this._buildReadingControls(location, provider)}
                ${this._buildProviderRefreshStatus(provider)}
                ${this._buildManualInvoiceStatus(location, provider, index)}
              </div>
            `
            : ""
        }
      </div>
    `;
  }



  _getProviderAppLabel(provider) {
    const key = String(provider?.furnizor || "").trim().toLowerCase();
    const labels = {
      digi: "App. Digi",
      eon: "App. E.ON",
      hidroelectrica: "App. Hidroelectrica",
      myelectrica: "App. myElectrica",
      nova: "App. Nova",
      ebloc: "App. e-bloc",
      orange: "App. Orange",
      rervest: "App. RER Vest",
      comprest: "Portal Comprest",
      apa_oradea: "Portal Apă Oradea",
      hidro_prahova: "Portal Hidro Prahova",
    };
    return labels[key] || "";
  }

  _buildProviderOpenAppButton(provider) {
    const providerKey = String(provider?.furnizor || "").trim().toLowerCase();
    const label = this._getProviderAppLabel(provider);
    if (!providerKey || !label) return "";

    return `<button class="provider-app-btn" data-provider-open="${this._escapeAttr(providerKey)}">${this._escapeHtml(label)}</button>`;
  }

  _findEntityByEntityId(entityId) {
    if (!entityId) return null;
    return this._hass?.states?.[entityId] || null;
  }

  _findEntityByFriendlyName(domain, names) {
    const wanted = (names || []).map((x) => String(x || "").trim().toLowerCase()).filter(Boolean);
    if (!wanted.length || !this._hass?.states) return null;

    for (const [entityId, stateObj] of Object.entries(this._hass.states)) {
      if (!entityId.startsWith(`${domain}.`)) continue;
      const friendly = String(stateObj?.attributes?.friendly_name || "").trim().toLowerCase();
      if (!friendly) continue;
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

  _getLicenseData() {
    const statusEntity = this._resolveEntity(
      "sensor",
      [
        "sensor.utilitati_romania_status_licenta",
        "sensor.administrare_integrare_status_licenta",
        "sensor.status_licenta",
      ],
      ["status licență"]
    );

    const planEntity = this._resolveEntity(
      "sensor",
      [
        "sensor.utilitati_romania_plan_licenta",
        "sensor.administrare_integrare_plan_licenta",
        "sensor.plan_licenta",
      ],
      ["plan licență"]
    );

    const expiresEntity = this._resolveEntity(
      "sensor",
      [
        "sensor.utilitati_romania_expira_la",
        "sensor.utilitati_romania_valabila_pana_la",
        "sensor.administrare_integrare_valabila_pana_la",
        "sensor.valabila_pana_la",
        "sensor.valabil_pana_la",
        "sensor.expira_la",
      ],
      ["valabilă până la", "expiră la"]
    );

    const checkedEntity = this._resolveEntity(
      "sensor",
      [
        "sensor.utilitati_romania_ultima_verificare_licenta",
        "sensor.administrare_integrare_ultima_verificare_licenta",
        "sensor.ultima_verificare_licenta",
      ],
      ["ultima verificare licență"]
    );

    const userEntity = this._resolveEntity(
      "sensor",
      [
        "sensor.utilitati_romania_cont_licenta",
        "sensor.administrare_integrare_cont_licenta",
        "sensor.cont_licenta",
        "sensor.utilitati_romania_utilizator_licenta",
      ],
      ["cont licență"]
    );

    const messageEntity = this._resolveEntity(
      "sensor",
      [
        "sensor.utilitati_romania_mesaj_licenta",
        "sensor.administrare_integrare_mesaj_licenta",
        "sensor.mesaj_licenta",
      ],
      ["mesaj licență"]
    );

    const inputEntity = this._resolveEntity(
      "text",
      [
        "text.utilitati_romania_cod_licenta_noua",
        "text.administrare_integrare_cod_licenta_noua",
        "text.cod_licenta_noua",
      ],
      ["cod licență nou", "licență nouă", "cod licență"]
    );

    const applyButtonEntity = this._resolveEntity(
      "button",
      [
        "button.utilitati_romania_aplica_licenta",
        "button.administrare_integrare_aplica_licenta",
        "button.aplica_licenta",
      ],
      ["aplică licență"]
    );

    const hasVisibleData = !!statusEntity || !!planEntity || !!expiresEntity || !!checkedEntity || !!userEntity || !!messageEntity || !!inputEntity || !!applyButtonEntity;

    return {
      hasVisibleData,
      status: statusEntity?.state || null,
      plan: planEntity?.state || null,
      expires: expiresEntity?.state || null,
      checkedAt: checkedEntity?.state || null,
      user: userEntity?.state || null,
      message: messageEntity?.state || null,
      inputEntityId: inputEntity?.entity_id || null,
      inputValue: inputEntity?.state || "",
      applyButtonEntityId: applyButtonEntity?.entity_id || null,
    };
  }

  _licenseStatusClass(statusValue) {
    const value = String(statusValue || "").trim().toLowerCase();
    if (["active", "activ", "lifetime", "valid"].includes(value)) return "status-paid";
    if (["trial", "grace"].includes(value)) return "status-credit";
    if (["expired", "invalid", "inactive", "inactiv"].includes(value)) return "status-unpaid";
    return "status-unknown";
  }

  _buildLicenseSection() {
    if (!this._config.show_license) return "";

    const data = this._getLicenseData();
    if (!data.hasVisibleData) return "";

    this._licenseInputEntityId = data.inputEntityId;
    this._licenseApplyEntityId = data.applyButtonEntityId;
    if (!this._licenseInputValue) this._licenseInputValue = data.inputValue || "";

    const action = data.applyButtonEntityId ? this._getActionState("license", data.applyButtonEntityId) : { status: "idle", message: "" };
    const statusText = data.status || "—";
    const statusClass = this._licenseStatusClass(data.status);
    const planText = data.plan || "—";
    const expiresText = data.expires ? this._formatDate(data.expires) : "—";
    const checkedText = data.checkedAt ? this._formatDateTime(data.checkedAt) : "—";
    const userText = data.user || "—";
    const messageText = data.message && data.message !== "-" ? data.message : null;

    return `
      <div class="license-wrap">
        <div class="license-header">
          <div class="license-heading">
            <div class="license-title">Licență</div>
            <div class="license-subtitle">${this._escapeHtml(statusText)}</div>
          </div>
          <button class="details-btn license-toggle-btn">
            ${this._licenseExpanded ? "Ascunde" : "Detalii"}
          </button>
        </div>

        ${
          this._licenseExpanded
            ? `
              <div class="license-details">
                <div><span class="detail-label">Status:</span> <span class="detail-value ${statusClass}">${this._escapeHtml(statusText)}</span></div>
                <div><span class="detail-label">Plan:</span> <span class="detail-value">${this._escapeHtml(planText)}</span></div>
                <div><span class="detail-label">Valabilă până la:</span> <span class="detail-value">${this._escapeHtml(expiresText)}</span></div>
                <div><span class="detail-label">Ultima verificare:</span> <span class="detail-value">${this._escapeHtml(checkedText)}</span></div>
                <div><span class="detail-label">Cont licență:</span> <span class="detail-value">${this._escapeHtml(userText)}</span></div>
                ${messageText ? `<div><span class="detail-label">Mesaj:</span> <span class="detail-value">${this._escapeHtml(messageText)}</span></div>` : ""}
                ${
                  data.inputEntityId && data.applyButtonEntityId
                    ? `
                      <div class="license-inline-editor">
                        <input
                          class="license-input"
                          type="text"
                          spellcheck="false"
                          autocomplete="off"
                          placeholder="Introduceți codul licenței"
                          value="${this._escapeAttr(this._licenseInputValue)}"
                          ${action.status === "sending" ? "disabled" : ""}
                        />
                        <button class="license-btn apply-license-btn" ${action.status === "sending" ? "disabled" : ""}>${action.status === "sending" ? "Se aplică..." : "Aplică"}</button>
                      </div>
                    `
                    : ""
                }
                ${
                  action.status === "success"
                    ? `<div class="inline-status status-success">${this._escapeHtml(action.message || "Licența a fost actualizată.")}</div>`
                    : action.status === "error"
                      ? `<div class="inline-status status-error">${this._escapeHtml(action.message || "Licența nu a putut fi actualizată.")}</div>`
                      : ""
                }
              </div>
            `
            : ""
        }
      </div>
    `;
  }

  _attachEvents(root) {
    const groupingSelect = root.querySelector(".invoice-grouping-select");
    if (groupingSelect) {
      const markSelectActive = () => {
        this._invoiceGroupingSelectActive = true;
      };
      const releaseSelectActive = () => {
        window.setTimeout(() => {
          this._invoiceGroupingSelectActive = false;
        }, 250);
      };

      groupingSelect.addEventListener("pointerdown", markSelectActive);
      groupingSelect.addEventListener("mousedown", markSelectActive);
      groupingSelect.addEventListener("touchstart", markSelectActive, { passive: true });
      groupingSelect.addEventListener("focus", markSelectActive);
      groupingSelect.addEventListener("blur", releaseSelectActive);
      groupingSelect.addEventListener("change", (event) => {
        this._setInvoiceGrouping(event.target.value);
        this._invoiceGroupingSelectActive = false;
        window.setTimeout(() => this._render(), 0);
      });
    }

    root.querySelectorAll(".details-btn[data-key]").forEach((button) => {
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        const key = button.getAttribute("data-key");
        if (!key) return;
        this._expanded[key] = !this._expanded[key];
        this._render();
      });
    });

    root.querySelectorAll(".provider-app-btn[data-provider-open]").forEach((button) => {
      button.addEventListener("click", async (event) => {
        event.stopPropagation();
        const provider = button.getAttribute("data-provider-open");
        if (!provider) return;

        try {
          await this._hass.callService("utilitati_romania", "open_provider", {
            provider,
          });
        } catch (err) {
          console.error("Nu am putut deschide furnizorul", provider, err);
        }
      });
    });

    root.querySelectorAll(".pdf-btn[data-url]").forEach((button) => {
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        const url = button.getAttribute("data-url");
        if (url) window.open(url, "_blank", "noopener");
      });
    });
    root.querySelectorAll(".refresh-btn[data-refresh-entity]").forEach((button) => {
      button.addEventListener("click", async (event) => {
        event.stopPropagation();
        const refreshEntityId = button.getAttribute("data-refresh-entity");
        const aggregatedEntityId = this._findEntityId();
        if (!refreshEntityId) return;

        this._setActionState("refresh", refreshEntityId, { status: "sending", message: "" });
        this._render();

        try {
          await this._hass.callService("button", "press", {
            entity_id: refreshEntityId,
          });

          await this._sleep(1800);
          await this._refreshEntities([refreshEntityId, aggregatedEntityId]);

          this._readingCache.clear();
          this._setActionState("refresh", refreshEntityId, {
            status: "success",
            message: "Datele furnizorului au fost actualizate.",
          });
        } catch (err) {
          this._setActionState("refresh", refreshEntityId, {
            status: "error",
            message: err?.message || "Actualizarea furnizorului a eșuat.",
          });
        }

        this._render();
      });
    });


    root.querySelectorAll(".manual-status-btn[data-payload][data-key]").forEach((button) => {
      button.addEventListener("click", async (event) => {
        event.stopPropagation();
        const rowKey = button.getAttribute("data-key");
        const payloadRaw = button.getAttribute("data-payload");
        const aggregatedEntityId = this._findEntityId();
        if (!rowKey || !payloadRaw) return;

        let payload = null;
        try {
          payload = JSON.parse(payloadRaw);
        } catch (_err) {
          payload = null;
        }
        if (!payload) return;

        this._setActionState("manual_status", rowKey, { status: "sending", message: "" });
        this._render();

        try {
          await this._hass.callService("utilitati_romania", "set_invoice_status", payload);
          await this._sleep(500);
          await this._refreshEntities([aggregatedEntityId]);
          this._setActionState("manual_status", rowKey, {
            status: "success",
            message: payload.status === "paid" ? "Factura a fost marcată local ca plătită." : "Marcarea manuală a fost eliminată.",
          });
        } catch (err) {
          this._setActionState("manual_status", rowKey, {
            status: "error",
            message: err?.message || "Actualizarea statusului a eșuat.",
          });
        }

        this._render();
      });
    });

    const licenseToggle = root.querySelector(".license-toggle-btn");
    if (licenseToggle) {
      licenseToggle.addEventListener("click", (event) => {
        event.stopPropagation();
        this._licenseExpanded = !this._licenseExpanded;
        this._render();
      });
    }

    const licenseInput = root.querySelector(".license-input");
    if (licenseInput) {
      licenseInput.addEventListener("input", (event) => {
        this._licenseInputValue = event.target.value || "";
      });

      licenseInput.addEventListener("keydown", async (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          const applyBtn = root.querySelector(".apply-license-btn");
          if (applyBtn) applyBtn.click();
        }
      });
    }

    const applyLicenseBtn = root.querySelector(".apply-license-btn");
    if (applyLicenseBtn) {
      applyLicenseBtn.addEventListener("click", async (event) => {
        event.stopPropagation();
        const value = String(this._licenseInputValue || "").trim();
        if (!value || !this._licenseInputEntityId || !this._licenseApplyEntityId) return;

        this._setActionState("license", this._licenseApplyEntityId, { status: "sending", message: "" });
        this._render();

        try {
          await this._hass.callService("text", "set_value", {
            entity_id: this._licenseInputEntityId,
            value,
          });

          await this._hass.callService("button", "press", {
            entity_id: this._licenseApplyEntityId,
          });

          await this._sleep(1800);
          await this._refreshEntities([
            this._licenseInputEntityId,
            this._licenseApplyEntityId,
            "sensor.utilitati_romania_status_licenta",
            "sensor.utilitati_romania_plan_licenta",
            "sensor.utilitati_romania_valabila_pana_la",
            "sensor.utilitati_romania_ultima_verificare_licenta",
            "sensor.utilitati_romania_cont_licenta",
            "sensor.utilitati_romania_mesaj_licenta",
          ]);

          this._setActionState("license", this._licenseApplyEntityId, {
            status: "success",
            message: "Licența a fost actualizată. Datele se reîncarcă automat.",
          });
        } catch (err) {
          this._setActionState("license", this._licenseApplyEntityId, {
            status: "error",
            message: err?.message || "Aplicarea licenței a eșuat.",
          });
        }

        this._render();
      });
    }

    root.querySelectorAll(".reading-input").forEach((input) => {
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          const wrapper = event.target.closest(".reading-control");
          const button = wrapper?.querySelector(".reading-submit-btn");
          if (button) button.click();
        }
      });
    });

    root.querySelectorAll(".reading-submit-btn").forEach((button) => {
      button.addEventListener("click", async (event) => {
        event.stopPropagation();
        const wrapper = event.target.closest(".reading-control");
        const numberEntityId = wrapper?.getAttribute("data-number-entity");
        const buttonEntityId = wrapper?.getAttribute("data-button-entity");
        const currentEntityId = wrapper?.getAttribute("data-current-entity");
        const input = wrapper?.querySelector(".reading-input");
        const value = String(input?.value ?? "").trim();

        if (!numberEntityId || !buttonEntityId || !value) {
          if (buttonEntityId) {
            this._setActionState("reading", buttonEntityId, {
              status: "error",
              message: "Introdu mai întâi o valoare validă pentru index.",
            });
            this._render();
          }
          return;
        }

        const numericValue = this._toNumber(value);
        if (!Number.isFinite(numericValue) || numericValue <= 0) {
          this._setActionState("reading", buttonEntityId, {
            status: "error",
            message: "Introdu o valoare numerică validă pentru index.",
          });
          this._render();
          return;
        }

        const control = {
          numberEntityId,
          buttonEntityId,
          label: String(wrapper?.getAttribute("data-control-label") || "Index de transmis"),
          currentValue: String(wrapper?.getAttribute("data-current-value") || ""),
          unit: String(wrapper?.getAttribute("data-unit") || ""),
        };

        const currentNumeric = this._toNumber(control.currentValue);
        if (Number.isFinite(currentNumeric) && currentNumeric > 0 && numericValue < currentNumeric) {
          this._setActionState("reading", buttonEntityId, {
            status: "error",
            message: `Valoarea introdusă este mai mică decât indexul curent (${currentNumeric}).`,
          });
          this._render();
          return;
        }

        const lastSent = this._getBackendReadingHistoryEntry(control);
        if (lastSent) {
          const lastTimestamp = new Date(lastSent.timestamp).getTime();
          const ageMs = Date.now() - lastTimestamp;
          if (String(lastSent.value) === String(value) && Number.isFinite(ageMs) && ageMs < 2 * 60 * 1000) {
            this._setActionState("reading", buttonEntityId, {
              status: "error",
              message: "Aceeași valoare a fost trimisă foarte recent. Verifică înainte să retrimiți.",
            });
            this._render();
            return;
          }
        }

        const numberStateObj = this._hass.states[numberEntityId];
        const buttonStateObj = this._hass.states[buttonEntityId];
        if (!numberStateObj || !buttonStateObj) {
          this._setActionState("reading", buttonEntityId, {
            status: "error",
            message: "Entitățile pentru transmiterea indexului nu sunt disponibile. Repornește Home Assistant după update și verifică entitățile număr/buton ale furnizorului.",
          });
          this._render();
          return;
        }

        this._setActionState("reading", buttonEntityId, { status: "sending", message: "" });
        this._render();

        try {
          const providerKey = String(wrapper?.getAttribute("data-provider") || "").trim().toLowerCase();
          if (providerKey === "apa_canal" || providerKey === "apa_brasov" || providerKey === "apa_oradea" || providerKey === "hidro_prahova") {
            await this._hass.callService("utilitati_romania", "submit_reading", {
              provider: providerKey,
              entry_id: String(wrapper?.getAttribute("data-entry-id") || ""),
              id_cont: String(wrapper?.getAttribute("data-id-cont") || ""),
              id_contract: String(wrapper?.getAttribute("data-id-contract") || ""),
              value: numericValue,
            });
          } else {
            if (providerKey === "eon") {
              const wrongProviderPattern = /(hidro|hidroelectrica|myelectrica|apa_canal|apa_brasov|apa_oradea|hidro_prahova|apacanal|ebloc)/i;
              if (wrongProviderPattern.test(String(numberEntityId)) || wrongProviderPattern.test(String(buttonEntityId))) {
                throw new Error("Cardul a identificat o entitate de la alt furnizor pentru E.ON. Reîncarcă pagina sau actualizează integrarea.");
              }
            }

            await this._hass.callService("number", "set_value", {
              entity_id: numberEntityId,
              value: numericValue,
            });

            const valueSynced = await this._waitForNumberState(
              numberEntityId,
              numericValue,
              providerKey === "eon" ? 15000 : 6000
            );

            if (!valueSynced) {
              const currentState = this._hass?.states?.[numberEntityId]?.state ?? "necunoscut";
              throw new Error(
                `Valoarea introdusă nu a fost confirmată încă de Home Assistant pentru ${numberEntityId}. ` +
                `Valoare curentă: ${currentState}. Reîncearcă după câteva secunde.`
              );
            }

            await this._hass.callService("button", "press", {
              entity_id: buttonEntityId,
            });
          }

          await this._sleep(2000);
          await this._refreshEntities([
            numberEntityId,
            buttonEntityId,
            this._findEntityId(),
            currentEntityId,
          ]);

          this._readingCache.clear();
          this._setActionState("reading", buttonEntityId, {
            status: "success",
            message: providerKey === "eon"
              ? "Indexul a fost trimis către E.ON. Confirmarea apare și în notificările Home Assistant."
              : "Indexul a fost trimis. Cardul se actualizează automat.",
          });
        } catch (err) {
          this._setActionState("reading", buttonEntityId, {
            status: "error",
            message: err?.message || "Transmiterea indexului a eșuat.",
          });
        }

        this._render();
      });
    });
  }

  _render() {
    if (!this._hass) return;

    this._readingCache.clear();

    if (!this.content) {
      const card = document.createElement("ha-card");
      this.content = document.createElement("div");
      this.content.className = "card-content";
      card.appendChild(this.content);
      this.appendChild(card);
    }

    const entityId = this._findEntityId();
    const entity = entityId ? this._hass.states[entityId] : null;

    if (!entity) {
      this.content.innerHTML = `
        <style>${this._styles()}</style>
        <div class="wrapper">
          <div class="title">${this._escapeHtml(this._config.title)}</div>
          <div class="error">Nu am găsit senzorul agregat pentru facturi.</div>
        </div>
      `;
      return;
    }

    const attrs = entity.attributes || {};
    const locations = this._filterLocations(Array.isArray(attrs.locatii) ? attrs.locatii : []);
    const invoiceEntries = this._collectInvoiceEntries(locations);
    const grouping = this._getInvoiceGrouping();
    const invoiceCount = invoiceEntries.length;

    this.content.innerHTML = `
      <style>${this._styles()}</style>
      <div class="wrapper">
        ${
          this._config.show_header
            ? `
              <div class="header">
                <div class="header-left">
                  <img class="ur-logo" src="/utilitati_romania/logo.png" />
                  <div class="title">${this._escapeHtml(this._config.title || entity.attributes.friendly_name || "Facturi utilități")}</div>
                </div>
                <div class="count">${invoiceCount} ${invoiceCount === 1 ? "factură" : "facturi"} • ${locations.length} ${locations.length === 1 ? "adresă" : "adrese"}</div>
              </div>
            `
            : ""
        }

        ${this._buildLicenseSection()}

        ${this._config.show_summary ? this._buildSummary(attrs) : ""}

        ${this._buildInvoiceGroupingSelector(grouping, invoiceCount)}

        <div class="locations">
          ${
            invoiceEntries.length
              ? grouping === "location"
                ? this._renderInvoicesByLocation(locations)
                : this._renderInvoicesByGrouping(invoiceEntries, grouping)
              : `<div class="empty">Nu există facturi de afișat pentru filtrele selectate.</div>`
          }
        </div>

        <div class="footer">Card v${UTILITATI_ROMANIA_FRONTEND_VERSION} • Sursă date: ${this._escapeHtml(entity.entity_id)}</div>
      </div>
    `;

    this._attachEvents(this.content);
  }

  _styles() {
    return `
      ha-card {
        box-shadow: 0 6px 18px rgba(0, 0, 0, 0.25);
        border-radius: 14px;
      }

      .wrapper {
        padding: 16px;
      }

      .header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 12px;
        margin-bottom: 14px;
        padding: 12px;
        border-radius: 12px;
        background: rgba(255, 255, 255, 0.03);
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.18);
      }

      .header-left {
        display: flex;
        align-items: center;
        gap: 10px;
      }

      .ur-logo {
        width: 34px;
        height: 34px;
        object-fit: contain;
      }

      .title {
        font-size: 1.15rem;
        font-weight: 700;
      }

      .count {
        font-size: 0.85rem;
        color: var(--secondary-text-color);
      }

      .license-wrap {
        margin-bottom: 18px;
        border-radius: 12px;
        background: var(--secondary-background-color);
        overflow: hidden;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.18);
        border: 1px solid rgba(255, 255, 255, 0.05);
      }

      .license-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 10px;
        padding: 12px;
      }

      .license-heading {
        min-width: 0;
      }

      .license-title {
        font-size: 1rem;
        font-weight: 700;
      }

      .license-subtitle {
        font-size: 0.84rem;
        color: var(--secondary-text-color);
        margin-top: 2px;
      }

      .license-details {
        padding: 0 12px 12px 12px;
        display: flex;
        flex-direction: column;
        gap: 6px;
        font-size: 0.9rem;
        border-top: 1px solid rgba(255, 255, 255, 0.05);
      }

      .license-inline-editor {
        display: grid;
        grid-template-columns: minmax(0, 1fr) auto;
        gap: 8px;
        margin-top: 10px;
      }

      .license-input,
      .reading-input {
        width: 100%;
        min-width: 0;
        padding: 10px 12px;
        border-radius: 10px;
        border: 1px solid var(--divider-color);
        background: var(--card-background-color);
        color: var(--primary-text-color);
        font: inherit;
        outline: none;
        box-sizing: border-box;
      }

      .license-input:focus,
      .reading-input:focus {
        border-color: var(--primary-color);
      }

      .license-btn,
      .reading-submit-btn,
      .details-btn,
      .pdf-btn,
      .provider-app-btn,
      .refresh-btn {
        padding: 8px 12px;
        border-radius: 10px;
        border: 1px solid var(--divider-color);
        background: transparent;
        color: var(--primary-text-color);
        cursor: pointer;
        font: inherit;
      }

      .license-btn:hover,
      .reading-submit-btn:hover,
      .details-btn:hover,
      .pdf-btn:hover,
      .provider-app-btn:hover,
      .refresh-btn:hover {
        background: rgba(255, 255, 255, 0.04);
      }

      .license-btn:disabled,
      .reading-submit-btn:disabled,
      .refresh-btn:disabled {
        opacity: 0.6;
        cursor: default;
      }

      .badge-unpaid {
        display: inline-flex;
        align-items: center;
        padding: 3px 8px;
        border-radius: 999px;
        background: rgba(244, 67, 54, 0.15);
        color: var(--error-color);
        font-size: 0.7rem;
        font-weight: 700;
        letter-spacing: 0.02em;
      }

      .summary {
        display: flex;
        flex-direction: column;
        gap: 6px;
        margin-bottom: 18px;
        padding-bottom: 14px;
        border-bottom: 1px solid var(--divider-color);
      }

      .summary-label {
        color: var(--secondary-text-color);
      }

      .summary-value {
        font-weight: 600;
      }

      .invoice-toolbar {
        display: grid;
        grid-template-columns: auto minmax(160px, 1fr) auto;
        align-items: center;
        gap: 10px;
        margin-bottom: 16px;
        padding: 10px 12px;
        border-radius: 12px;
        background: rgba(255, 255, 255, 0.03);
        border: 1px solid rgba(255, 255, 255, 0.05);
      }

      .invoice-grouping-label {
        color: var(--secondary-text-color);
        font-size: 0.88rem;
        white-space: nowrap;
      }

      .invoice-grouping-select {
        width: 100%;
        min-width: 0;
        padding: 8px 10px;
        border-radius: 10px;
        border: 1px solid var(--divider-color);
        background: var(--card-background-color);
        color: var(--primary-text-color);
        font: inherit;
        outline: none;
      }

      .invoice-grouping-select:focus {
        border-color: var(--primary-color);
      }

      .invoice-grouping-hint {
        color: var(--secondary-text-color);
        font-size: 0.82rem;
        white-space: nowrap;
      }

      .locations {
        display: flex;
        flex-direction: column;
        gap: 18px;
      }

      .location {
        padding-bottom: 14px;
        border-bottom: 1px solid var(--divider-color);
      }
    
      .location-unpaid {
        border-left: 4px solid var(--error-color);
        padding-left: 10px;
      }

      .location-unpaid .location-title {
        color: var(--error-color);
      }

      .location:last-child {
        border-bottom: none;
        padding-bottom: 0;
      }

      .location-heading {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 10px;
        margin-bottom: 4px;
      }

      .location-title {
        font-size: 1rem;
        font-weight: 700;
      }

      .location-meta {
        font-size: 0.85rem;
        color: var(--secondary-text-color);
        margin-bottom: 12px;
      }

      .reading-badge {
        display: inline-flex;
        align-items: center;
        padding: 4px 8px;
        border-radius: 999px;
        background: rgba(33, 150, 243, 0.14);
        color: var(--primary-color);
        font-size: 0.75rem;
        font-weight: 600;
        white-space: nowrap;
      }

      .invoice-list {
        display: flex;
        flex-direction: column;
        gap: 10px;
      }

      .invoice-row-wrap {
        border-radius: 12px;
        background: var(--secondary-background-color);
        overflow: hidden;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.18);
        transition: transform 0.15s ease, box-shadow 0.15s ease;
      }

      .invoice-unpaid .row-amount {
        color: var(--error-color);
        font-weight: 700;
      }      

      .invoice-row-wrap:hover {
        transform: translateY(0px);
        box-shadow: 0 6px 16px rgba(0, 0, 0, 0.25);
      }

      .row-actions {
        display: flex;
        align-items: center;
        justify-content: flex-end;
        gap: 8px;
        flex-wrap: wrap;
      }

      .refresh-btn {
        white-space: nowrap;
      }

      .refresh-btn.icon-only {
        width: 38px;
        min-width: 38px;
        height: 38px;
        padding: 0;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        font-size: 1.05rem;
        line-height: 1;
      }

      .invoice-row {
        display: grid;
        grid-template-columns: minmax(0, 1.8fr) auto auto;
        gap: 10px;
        align-items: center;
        padding: 10px 12px;
      }

      .row-main {
        min-width: 0;
      }

      .row-supplier {
        font-weight: 700;
        margin-bottom: 2px;
        display: flex;
        align-items: center;
        gap: 8px;
        flex-wrap: wrap;
      }

      .row-title {
        font-size: 0.84rem;
        color: var(--secondary-text-color);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .row-utility {
        margin-top: 2px;
        font-size: 0.78rem;
        color: var(--secondary-text-color);
        font-weight: 600;
        opacity: 0.88;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .row-amount-block {
        display: flex;
        flex-direction: column;
        align-items: flex-end;
        gap: 2px;
        min-width: max-content;
      }

      .row-amount {
        font-weight: 600;
        white-space: nowrap;
      }

      .row-due {
        font-size: 0.78rem;
        font-weight: 500;
        color: var(--primary-text-color);
        white-space: nowrap;
      }

      .row-due-overdue {
        color: var(--error-color);
        font-weight: 700;
      }

      .row-due-soon {
        color: var(--info-color, var(--primary-color));
        font-weight: 700;
      }

      .details-btn {
        white-space: nowrap;
      }

      .invoice-details {
        padding: 0 12px 12px 12px;
        display: flex;
        flex-direction: column;
        gap: 6px;
        font-size: 0.9rem;
        border-top: 1px solid rgba(255, 255, 255, 0.05);
      }

      .detail-label {
        color: var(--secondary-text-color);
      }

      .detail-value {
        font-weight: 500;
      }

      .detail-actions {
        margin-top: 4px;
        display: flex;
        align-items: center;
        gap: 8px;
        flex-wrap: wrap;
      }

      .reading-wrap {
        margin-top: 10px;
        padding-top: 10px;
        border-top: 1px dashed rgba(255, 255, 255, 0.12);
      }

      .reading-title {
        font-weight: 700;
        margin-bottom: 4px;
      }

      .reading-period {
        font-size: 0.84rem;
        color: var(--secondary-text-color);
        margin-bottom: 10px;
      }

      .reading-control {
        padding: 10px;
        border-radius: 10px;
        background: rgba(255, 255, 255, 0.03);
        margin-top: 8px;
      }

      .reading-control-header {
        display: flex;
        justify-content: space-between;
        gap: 10px;
        align-items: center;
        margin-bottom: 8px;
      }

      .reading-control-title {
        font-weight: 600;
      }

      .reading-control-current {
        color: var(--secondary-text-color);
        font-size: 0.84rem;
        text-align: right;
      }

      .reading-control-editor {
        display: grid;
        grid-template-columns: minmax(0, 1fr) auto;
        gap: 8px;
      }

      .reading-last-sent {
        margin-top: 8px;
        font-size: 0.82rem;
        color: var(--secondary-text-color);
      }

      .inline-status {
        margin-top: 8px;
        font-size: 0.84rem;
        font-weight: 500;
      }

      .status-paid,
      .status-success {
        color: var(--success-color, #2e7d32);
      }

      .status-unpaid,
      .status-error {
        color: var(--error-color);
      }

      .status-credit {
        color: var(--warning-color, #f9a825);
      }

      .status-unknown {
        color: var(--secondary-text-color);
      }

      .empty,
      .error {
        color: var(--secondary-text-color);
      }

      .footer {
        margin-top: 16px;
        font-size: 0.78rem;
        color: var(--secondary-text-color);
      }

      @media (max-width: 640px) {
        .invoice-row {
          grid-template-columns: minmax(0, 1fr) auto;
        }

        .row-main {
          grid-column: 1;
          grid-row: 1;
        }

        .row-amount-block {
          grid-column: 1;
          grid-row: 2;
          align-items: flex-start;
          min-width: 0;
        }

        .row-actions {
          grid-column: 2;
          grid-row: 1 / span 2;
          align-self: center;
        }

        .row-amount {
          font-size: 0.9rem;
        }

        .row-due {
          white-space: normal;
        }

        .license-header,
        .reading-control-header,
        .location-heading {
          align-items: flex-start;
          flex-direction: column;
        }

        .license-inline-editor,
        .reading-control-editor {
          grid-template-columns: 1fr;
        }

        .reading-control-current {
          text-align: left;
        }
      }
    `;
  }

  static getStubConfig() {
    return {
      type: "custom:utilitati-romania-facturi-card",
      title: "Facturi utilități",
      show_header: true,
      show_summary: true,
      only_unpaid: false,
      show_paid: true,
      show_license: true,
    };
  }
}

if (!customElements.get("utilitati-romania-facturi-card")) {
  customElements.define("utilitati-romania-facturi-card", UtilitatiRomaniaFacturiCard);
}

window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === "utilitati-romania-facturi-card")) {
  window.customCards.push({
    type: "utilitati-romania-facturi-card",
    name: "Utilități România Facturi Card",
    description: "Card compact cu detalii expandabile pentru facturi agregate, licență și transmitere index.",
  });
}
