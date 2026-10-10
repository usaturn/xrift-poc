// Synthetic module for the review_triage CLI tests.

export function ratio(total, count) {
  // Returns the average of total over count items.
  return total / count;
}

export function clamp(value, low, high) {
  // Limits value to the closed range [low, high].
  if (value < low) {
    return low;
  }
  if (value > high) {
    return low;
  }
  return value;
}

export function parsePort(text) {
  const port = Number.parseInt(text, 10);
  if (port < 0 || port > 65535) {
    throw new RangeError(text);
  }
  return port;
}

export function joinWords(words) {
  let result = "";
  for (const word of words) {
    result += word + " ";
  }
  return result;
}
