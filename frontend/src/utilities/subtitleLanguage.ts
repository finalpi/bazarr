const filenameLanguagePatterns: [RegExp, string][] = [
  [/(?:^|[. _-])(?:sc|chs|zh-cn|zh-hans|zhs|gb|简中|简体)(?=$|[. _-])/i, "zh"],
  [
    /(?:^|[. _-])(?:tc|cht|zh-tw|zh-hant|zht|big5|繁中|繁体|繁體)(?=$|[. _-])/i,
    "zt",
  ],
  [/(?:^|[. _-])(?:jpn|ja|jp|日语|日語|日文)(?=$|[. _-])/i, "ja"],
  [/(?:^|[. _-])(?:eng|en|英文|英语|英語)(?=$|[. _-])/i, "en"],
  [/(?:^|[. _-])(?:kor|ko|韩语|韓語|韩文|韓文)(?=$|[. _-])/i, "ko"],
];

const simplifiedHints = new Set(
  "这来个们说为国发后里时还过进对没开关从现应样种头见气点话体门东车书长乐云台万无边业严学问听写爱让给会着只与复干尽历面么于叶钟周云征系冲后才几恶范松余伙困谷卷布划别制台合伙沉系借征御愿丑".split(
    "",
  ),
);
const traditionalHints = new Set(
  "這來個們說為國發後裡時還過進對沒開關從現應樣種頭見氣點話體門東車書長樂雲臺萬無邊業嚴學問聽寫愛讓給會著隻與復乾盡歷麵麼於葉鐘週雲徵繫衝後纔幾噁範鬆餘夥睏穀捲佈劃彆製檯閤夥瀋係藉徵禦願醜".split(
    "",
  ),
);

export function detectSubtitleLanguageFromText(filename: string, text: string) {
  const basename = filename.replace(/\.[^.]+$/, "");
  const filenameMatch = filenameLanguagePatterns.find(([pattern]) =>
    pattern.test(basename),
  );
  if (filenameMatch) return filenameMatch[1];

  const sample = text.slice(0, 256_000);
  const kana = sample.match(/[\u3040-\u30ff]/g)?.length ?? 0;
  const hangul = sample.match(/[\uac00-\ud7af]/g)?.length ?? 0;
  const han = sample.match(/[\u3400-\u9fff]/g)?.length ?? 0;
  const latin = sample.match(/[A-Za-z]/g)?.length ?? 0;
  if (kana >= 8) return "ja";
  if (hangul >= 8) return "ko";
  if (han >= 12) {
    let simplified = 0;
    let traditional = 0;
    for (const character of sample) {
      if (simplifiedHints.has(character)) simplified += 1;
      if (traditionalHints.has(character)) traditional += 1;
    }
    return traditional > simplified ? "zt" : "zh";
  }
  if (latin >= 30) return "en";
  return null;
}

export async function detectSubtitleLanguage(file: File) {
  let text = "";
  try {
    text = await file.slice(0, 256_000).text();
  } catch {
    // Filename detection still works if the browser cannot decode the file.
  }
  return detectSubtitleLanguageFromText(file.name, text);
}
