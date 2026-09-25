// The "set an image" dialog, shared by tags, folders and videos:
// choose a photo from this device, or paste a URL for the server to download.
import { api, h } from "./api.js";

let dialog = null;
let options = null;

/**
 * Open the dialog.
 *   title      heading, e.g. `Image for "family"`
 *   uploadUrl  PUT the chosen file here
 *   urlUrl     POST { url } here to have the server download it
 *   removeUrl  DELETE here to remove the current image (omit when there is none)
 *   onDone     called after the image is set or removed
 */
export function openImageDialog(opts) {
  if (!dialog) dialog = build();
  options = opts;
  dialog.querySelector("h3").textContent = opts.title;
  dialog.querySelector("input[type=url]").value = "";
  dialog.querySelector(".remove").hidden = !opts.removeUrl;
  showError();
  setBusy();
  dialog.showModal();
}

function showError(message) {
  const error = dialog.querySelector(".error");
  error.textContent = message || "";
  error.hidden = !message;
}

function setBusy(message) {
  const status = dialog.querySelector(".status");
  status.textContent = message || "";
  status.hidden = !message;
  for (const el of dialog.querySelectorAll("button, input")) el.disabled = Boolean(message);
}

async function run(request, busyText) {
  showError();
  setBusy(busyText);
  try {
    await request();
    dialog.close();
    options.onDone?.();
  } catch (err) {
    showError(err.message);
  } finally {
    setBusy();
  }
}

function build() {
  const picker = h("input", {
    type: "file",
    accept: "image/jpeg,image/png,image/webp,image/gif,image/bmp",
    hidden: true,
    onchange: () => {
      const file = picker.files[0];
      picker.value = ""; // so choosing the same file again still triggers
      if (file) run(() => api("PUT", options.uploadUrl, file), "Saving image…");
    },
  });
  const urlInput = h("input", {
    type: "url",
    placeholder: "https://example.com/picture.jpg",
    "aria-label": "Image web address",
    autocomplete: "off",
    spellcheck: false,
  });

  const el = h(
    "dialog",
    { class: "image-dialog" },
    h("h3"),
    h(
      "button",
      { type: "button", class: "btn primary wide", onclick: () => picker.click() },
      "Choose a photo from this device",
    ),
    picker,
    h("div", { class: "or" }, h("span", {}, "or")),
    h(
      "form",
      {
        class: "url-form",
        onsubmit: (event) => {
          event.preventDefault();
          const url = urlInput.value.trim();
          if (!url) return showError("Paste the web address of an image.");
          run(() => api("POST", options.urlUrl, { url }), "Downloading image…");
        },
      },
      h("label", { class: "field" }, h("span", {}, "Image URL"), urlInput),
      h("button", { type: "submit", class: "btn" }, "Use this URL"),
    ),
    h("p", { class: "hint" }, "JPG, PNG, WebP, GIF or BMP, up to 20 MB."),
    h("p", { class: "hint status", hidden: true }),
    h("p", { class: "error", hidden: true }),
    h(
      "div",
      { class: "dialog-actions" },
      h(
        "button",
        {
          type: "button",
          class: "btn danger remove",
          onclick: () => run(() => api("DELETE", options.removeUrl), "Removing image…"),
        },
        "Remove image",
      ),
      h("div", { class: "spacer" }),
      h("button", { type: "button", class: "btn", onclick: () => el.close() }, "Cancel"),
    ),
  );
  document.body.append(el);
  return el;
}
