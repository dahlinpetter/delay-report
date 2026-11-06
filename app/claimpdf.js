// Fills in Skånetrafiken's paper claim form (claim-form.pdf) on the phone.
// Nothing here is sent anywhere: the personal details and the signature only
// exist on the device and in the PDF the user downloads.
(function (root) {
  "use strict";
  const H = 841.89; // page height in points; positions below are measured from the top

  // Cell centres, measured from the official form
  const PNR_CELL = i => 35.5 + 13.8 * i;      // 13 cells: YYYYMMDD - XXXX (cell 8 is the printed dash)
  const DATE_CELL = i => 312.3 + 19.37 * i;   // 10 cells: YYYY-MM-DD (cells 4 and 7 are printed dashes)
  const TICKET_CELL = i => 41.7 + 26.4 * i;   // 10 cells for Biljett-ID

  const RADIO = {
    d20: [34.3, 322.9], d40: [34.3, 335.1], d60: [120.4, 322.9], d120: [120.4, 335.1],
    priceCut: [233.9, 319.0],
    codeSms: [34.3, 376.6], codeEmail: [34.3, 388.8], bank: [34.3, 401.1]
  };
  const CHECK = { truthful: [28.2, 522.9], terms: [230.6, 522.9] };
  const SIGN = { x: 412, top: 500, w: 152, h: 38 };

  // 12-digit personnummer (YYYYMMDDXXXX) from what the user typed
  function pnr12(s) {
    const d = String(s || "").replace(/\D/g, "");
    if (d.length === 12) return d;
    if (d.length === 10) {
      const yy = Number(d.slice(0, 2)), now = new Date().getFullYear() % 100;
      const plus = /\+/.test(String(s)); // "+" means 100 years or older
      const century = (yy > now ? 19 : 20) - (plus ? 1 : 0);
      return String(century) + d;
    }
    return "";
  }
  function bandOf(min) {
    return min >= 120 ? "d120" : min >= 60 ? "d60" : min >= 40 ? "d40" : "d20";
  }

  async function make(opts) {
    const { PDFDocument, StandardFonts, rgb } = root.PDFLib;
    const bytes = await fetch("claim-form.pdf", { cache: "no-cache" }).then(r => {
      if (!r.ok) throw new Error("Couldn't load the claim form");
      return r.arrayBuffer();
    });
    const doc = await PDFDocument.load(bytes);
    const page = doc.getPages()[0];
    const font = await doc.embedFont(StandardFonts.Helvetica);
    const ink = rgb(0.05, 0.1, 0.35);

    const text = (s, x, top, size) => {
      s = String(s == null ? "" : s).trim();
      if (!s) return;
      size = size || 9.5;
      page.drawText(s, { x, y: H - top, size, font, color: ink });
    };
    const fit = (s, x, top, width, size) => {
      s = String(s == null ? "" : s).trim();
      if (!s) return;
      size = size || 9.5;
      while (size > 6 && font.widthOfTextAtSize(s, size) > width) size -= 0.5;
      page.drawText(s, { x, y: H - top, size, font, color: ink });
    };
    const cell = (ch, cx, top, size) => {
      size = size || 10;
      const w = font.widthOfTextAtSize(ch, size);
      page.drawText(ch, { x: cx - w / 2, y: H - top, size, font, color: ink });
    };
    const radio = key => {
      const [x, top] = RADIO[key];
      page.drawCircle({ x: x + 3.9, y: H - (top + 3.9), size: 2.4, color: ink });
    };
    const check = key => {
      const [x, top] = CHECK[key], s = 7.3, p = 1.3, y = H - top;
      page.drawLine({ start: { x: x + p, y: y - p }, end: { x: x + s - p, y: y - s + p }, thickness: 1.1, color: ink });
      page.drawLine({ start: { x: x + p, y: y - s + p }, end: { x: x + s - p, y: y - p }, thickness: 1.1, color: ink });
    };

    const me = opts.me || {}, t = opts.trip;

    // Personuppgifter
    const p = pnr12(me.pnr);
    if (p) {
      for (let i = 0; i < 8; i++) cell(p[i], PNR_CELL(i), 79);
      for (let i = 0; i < 4; i++) cell(p[8 + i], PNR_CELL(9 + i), 79);
    }
    fit(me.first, 212, 79, 172);
    fit(me.last, 391, 79, 172);
    fit(me.street, 32, 101.5, 174);
    fit(me.zip, 212, 101.5, 84);
    fit(me.city, 301, 101.5, 122);
    fit(me.country || "Sverige", 429, 101.5, 134);
    fit(me.phone, 32, 124, 174);
    fit(me.email, 211, 124, 352);

    // Biljettinformation
    const no = String(opts.ticketNo || "").trim();
    if (no) {
      if (opts.ticketKind === "card") fit(no, 32, 187.5, 262);
      else if (no.length <= 10) for (let i = 0; i < no.length; i++) cell(no[i], TICKET_CELL(i), 165);
      else fit(no, 32, 165, 262);
    }

    // Jag skulle resa
    const date = String(t.date).replace(/\D/g, "");
    [0, 1, 2, 3].forEach(i => cell(date[i], DATE_CELL(i), 165));
    [4, 5].forEach(i => cell(date[i], DATE_CELL(i + 1), 165));
    [6, 7].forEach(i => cell(date[i], DATE_CELL(i + 2), 165));
    fit(t.from, 306, 187.5, 188);
    text(t.dep, 500, 187.5);
    fit(t.to, 306, 210, 188);
    text(t.arr, 500, 210);

    // Försening, ersättning, utbetalning
    radio(bandOf(t.delay));
    radio("priceCut");
    if (opts.payout === "code") radio(me.email ? "codeEmail" : "codeSms");
    else radio("bank");

    if (opts.truthful) check("truthful");
    if (opts.terms) check("terms");

    if (opts.signature) {
      const img = await doc.embedPng(opts.signature);
      const sc = Math.min(SIGN.w / img.width, SIGN.h / img.height);
      const w = img.width * sc, h = img.height * sc;
      page.drawImage(img, { x: SIGN.x + (SIGN.w - w) / 2, y: H - SIGN.top - SIGN.h + (SIGN.h - h) / 2, width: w, height: h });
    }

    doc.setTitle("Förseningsersättning " + t.date);
    doc.setCreator("Backtrack");
    return doc.save();
  }

  root.BacktrackClaimPDF = { make, pnr12 };
  if (typeof module !== "undefined" && module.exports) module.exports = { make, pnr12, setLib: l => (root.PDFLib = l) };
})(typeof window !== "undefined" ? window : globalThis);
