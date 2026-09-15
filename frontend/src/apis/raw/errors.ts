export function getErrorMessage(
  data: unknown,
  defaultMsg = "Unknown error",
): string {
  if (typeof data === "string") {
    return data;
  }
  if (typeof data === "object" && data !== null) {
    const payload = data as Record<string, unknown>;
    const message =
      typeof payload.message === "string" ? payload.message : undefined;
    const errors = payload.errors;
    if (typeof errors === "object" && errors !== null) {
      const details = Object.values(errors as Record<string, unknown>)
        .flatMap((value) => (Array.isArray(value) ? value : [value]))
        .filter((value): value is string => typeof value === "string");
      if (details.length > 0) {
        return message ? `${message}: ${details.join("; ")}` : details.join("; ");
      }
    }
    if (message) {
      return message;
    }
  }
  return defaultMsg;
}
