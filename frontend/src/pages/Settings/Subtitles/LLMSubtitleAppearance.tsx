import { FunctionComponent } from "react";
import { ColorInput, SimpleGrid, Stack, Title } from "@mantine/core";
import { Check, CollapseBox, Number, Text } from "@/pages/Settings/components";
import {
  useBaseInput,
  useSettingValue,
} from "@/pages/Settings/utilities/hooks";
import styles from "./LLMSubtitleAppearance.module.scss";

const ColorSetting: FunctionComponent<{
  label: string;
  settingKey: string;
  fallback?: string;
}> = ({ fallback = "#FFFFFF", ...props }) => {
  const { value, update } = useBaseInput<{ label: string }, string>(props);

  return (
    <ColorInput
      label={props.label}
      value={value ?? fallback}
      format="hex"
      swatches={["#FFFFFF", "#FFFF80", "#FFE66D", "#7FDBFF", "#98FB98"]}
      onChange={update}
    />
  );
};

const LLMSubtitleAppearance: FunctionComponent = () => {
  const chineseFontName = useSettingValue<string>(
    "settings-translator-openai_ass_chinese_font_name",
  );
  const chineseFontSize = useSettingValue<number>(
    "settings-translator-openai_ass_chinese_font_size",
  );
  const chineseColor = useSettingValue<string>(
    "settings-translator-openai_ass_chinese_primary_color",
  );
  const chineseOutlineColor = useSettingValue<string>(
    "settings-translator-openai_ass_chinese_outline_color",
  );
  const chineseBold = useSettingValue<boolean>(
    "settings-translator-openai_ass_chinese_bold",
  );
  const chineseOutline = useSettingValue<number>(
    "settings-translator-openai_ass_chinese_outline",
  );
  const chineseShadow = useSettingValue<number>(
    "settings-translator-openai_ass_chinese_shadow",
  );
  const originalFontName = useSettingValue<string>(
    "settings-translator-openai_ass_original_font_name",
  );
  const originalFontSize = useSettingValue<number>(
    "settings-translator-openai_ass_original_font_size",
  );
  const originalColor = useSettingValue<string>(
    "settings-translator-openai_ass_original_primary_color",
  );
  const originalOutlineColor = useSettingValue<string>(
    "settings-translator-openai_ass_original_outline_color",
  );
  const originalBold = useSettingValue<boolean>(
    "settings-translator-openai_ass_original_bold",
  );
  const originalOutline = useSettingValue<number>(
    "settings-translator-openai_ass_original_outline",
  );
  const originalShadow = useSettingValue<number>(
    "settings-translator-openai_ass_original_shadow",
  );
  const margin = useSettingValue<number>(
    "settings-translator-openai_ass_bilingual_margin_v",
  );

  const previewTextStyle = (
    fontSize: number | null | undefined,
    fontName: string | null | undefined,
    color: string | null | undefined,
    outlineColor: string | null | undefined,
    bold: boolean | null | undefined,
    outline: number | null | undefined,
    shadow: number | null | undefined,
    fallbackSize: number,
    fallbackColor: string,
  ) => ({
    color: color ?? fallbackColor,
    fontFamily: `${fontName || "sans-serif"}, sans-serif`,
    fontSize: Math.max(13, Math.min(38, (fontSize ?? fallbackSize) * 0.4)),
    fontWeight: bold ? 700 : 400,
    WebkitTextStroke: `${Math.max(0, (outline ?? 0) * 0.4)}px ${outlineColor ?? "#000000"}`,
    paintOrder: "stroke fill",
    textShadow:
      (shadow ?? 0) > 0
        ? `${(shadow ?? 0) * 0.8}px ${(shadow ?? 0) * 0.8}px ${(shadow ?? 0) * 0.8}px ${outlineColor ?? "#000000"}`
        : "none",
  });

  return (
    <Stack gap="md">
      <Check
        label="Save OpenAI-compatible LLM subtitles as styled ASS"
        settingKey="settings-translator-openai_styled_ass"
      />
      <CollapseBox indent settingKey="settings-translator-openai_styled_ass">
        <div className={styles.preview} aria-label="Subtitle style preview">
          <div className={styles.shade} />
          <div
            className={styles.caption}
            style={{
              bottom: `${Math.max(4, Math.min(24, (margin ?? 60) / 8))}%`,
            }}
          >
            <span
              style={previewTextStyle(
                chineseFontSize,
                chineseFontName,
                chineseColor,
                chineseOutlineColor,
                chineseBold,
                chineseOutline,
                chineseShadow,
                52,
                "#FFFF80",
              )}
            >
              我们应该趁天黑前离开。
            </span>
            <span
              style={previewTextStyle(
                originalFontSize,
                originalFontName,
                originalColor,
                originalOutlineColor,
                originalBold,
                originalOutline,
                originalShadow,
                32,
                "#FFFFFF",
              )}
            >
              We should leave before it gets dark.
            </span>
          </div>
        </div>

        <Title order={4}>Chinese subtitle</Title>
        <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="md">
          <Text
            label="Font family"
            settingKey="settings-translator-openai_ass_chinese_font_name"
          />
          <Number
            label="Font size"
            settingKey="settings-translator-openai_ass_chinese_font_size"
            min={12}
            max={120}
          />
          <ColorSetting
            label="Text color"
            settingKey="settings-translator-openai_ass_chinese_primary_color"
            fallback="#FFFF80"
          />
          <ColorSetting
            label="Outline color"
            settingKey="settings-translator-openai_ass_chinese_outline_color"
            fallback="#000000"
          />
          <Number
            label="Outline width"
            settingKey="settings-translator-openai_ass_chinese_outline"
            min={0}
            max={10}
          />
          <Number
            label="Shadow depth"
            settingKey="settings-translator-openai_ass_chinese_shadow"
            min={0}
            max={10}
          />
          <Check
            label="Bold text"
            settingKey="settings-translator-openai_ass_chinese_bold"
          />
        </SimpleGrid>

        <Title order={4}>Original subtitle</Title>
        <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="md">
          <Text
            label="Font family"
            settingKey="settings-translator-openai_ass_original_font_name"
          />
          <Number
            label="Font size"
            settingKey="settings-translator-openai_ass_original_font_size"
            min={12}
            max={120}
          />
          <ColorSetting
            label="Text color"
            settingKey="settings-translator-openai_ass_original_primary_color"
          />
          <ColorSetting
            label="Outline color"
            settingKey="settings-translator-openai_ass_original_outline_color"
            fallback="#000000"
          />
          <Number
            label="Outline width"
            settingKey="settings-translator-openai_ass_original_outline"
            min={0}
            max={10}
          />
          <Number
            label="Shadow depth"
            settingKey="settings-translator-openai_ass_original_shadow"
            min={0}
            max={10}
          />
          <Check
            label="Bold text"
            settingKey="settings-translator-openai_ass_original_bold"
          />
        </SimpleGrid>

        <Number
          label="Bottom margin"
          settingKey="settings-translator-openai_ass_bilingual_margin_v"
          min={0}
          max={300}
        />
      </CollapseBox>
    </Stack>
  );
};

export default LLMSubtitleAppearance;
