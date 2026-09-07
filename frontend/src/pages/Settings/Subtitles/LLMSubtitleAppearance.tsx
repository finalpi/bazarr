import { FunctionComponent } from "react";
import { ColorInput, SimpleGrid, Stack } from "@mantine/core";
import { Check, CollapseBox, Number, Text } from "@/pages/Settings/components";
import {
  useBaseInput,
  useSettingValue,
} from "@/pages/Settings/utilities/hooks";
import styles from "./LLMSubtitleAppearance.module.scss";

const ColorSetting: FunctionComponent<{ label: string; settingKey: string }> = (
  props,
) => {
  const { value, update } = useBaseInput<{ label: string }, string>(props);

  return (
    <ColorInput
      label={props.label}
      value={value ?? "#FFFFFF"}
      format="hex"
      swatches={["#FFFFFF", "#FFE66D", "#7FDBFF", "#98FB98", "#000000"]}
      onChange={update}
    />
  );
};

const LLMSubtitleAppearance: FunctionComponent = () => {
  const fontName = useSettingValue<string>(
    "settings-translator-openai_ass_font_name",
  );
  const fontSize = useSettingValue<number>(
    "settings-translator-openai_ass_font_size",
  );
  const primaryColor = useSettingValue<string>(
    "settings-translator-openai_ass_primary_color",
  );
  const outlineColor = useSettingValue<string>(
    "settings-translator-openai_ass_outline_color",
  );
  const bold = useSettingValue<boolean>("settings-translator-openai_ass_bold");
  const outline = useSettingValue<number>(
    "settings-translator-openai_ass_outline",
  );
  const shadow = useSettingValue<number>(
    "settings-translator-openai_ass_shadow",
  );
  const margin = useSettingValue<number>(
    "settings-translator-openai_ass_margin_v",
  );

  const previewSize = Math.max(16, Math.min(42, (fontSize ?? 52) * 0.55));
  const stroke = Math.max(0, (outline ?? 3) * 0.65);
  const shadowSize = Math.max(0, (shadow ?? 1) * 0.8);

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
              bottom: `${Math.max(4, Math.min(24, (margin ?? 54) / 8))}%`,
              color: primaryColor ?? "#FFFFFF",
              fontFamily: `${fontName || "Noto Sans CJK SC"}, sans-serif`,
              fontSize: previewSize,
              fontWeight: bold ? 700 : 400,
              WebkitTextStroke: `${stroke}px ${outlineColor ?? "#000000"}`,
              textShadow:
                shadowSize > 0
                  ? `${shadowSize}px ${shadowSize}px ${shadowSize}px ${outlineColor ?? "#000000"}`
                  : "none",
            }}
          >
            <span>We should leave before it gets dark.</span>
            <span>我们应该趁天黑前离开。</span>
          </div>
        </div>
        <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="md">
          <Text
            label="Font family"
            settingKey="settings-translator-openai_ass_font_name"
          />
          <Number
            label="Font size"
            settingKey="settings-translator-openai_ass_font_size"
            min={12}
            max={120}
          />
          <ColorSetting
            label="Text color"
            settingKey="settings-translator-openai_ass_primary_color"
          />
          <ColorSetting
            label="Outline color"
            settingKey="settings-translator-openai_ass_outline_color"
          />
          <Number
            label="Outline width"
            settingKey="settings-translator-openai_ass_outline"
            min={0}
            max={10}
          />
          <Number
            label="Shadow depth"
            settingKey="settings-translator-openai_ass_shadow"
            min={0}
            max={10}
          />
          <Number
            label="Bottom margin"
            settingKey="settings-translator-openai_ass_margin_v"
            min={0}
            max={300}
          />
          <Check
            label="Bold text"
            settingKey="settings-translator-openai_ass_bold"
          />
        </SimpleGrid>
      </CollapseBox>
    </Stack>
  );
};

export default LLMSubtitleAppearance;
