export function createFormData(object?: LooseObject) {
  if (!object) {
    return undefined;
  }

  const form = new FormData();

  for (const key in object) {
    const data = object[key];
    // FormData coerces undefined to the literal string "undefined". Optional
    // numeric fields such as embeddedTrackId then fail Flask's int parser.
    if (data === undefined) {
      continue;
    }
    if (data instanceof Array) {
      if (data.length > 0) {
        data.forEach((val) => form.append(key, val));
      } else {
        form.append(key, "");
      }
    } else {
      form.append(key, data);
    }
  }

  return form;
}
