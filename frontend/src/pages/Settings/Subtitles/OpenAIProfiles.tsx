import { FunctionComponent, useMemo } from "react";
import { Stack } from "@mantine/core";
import { Message, Password, Selector, Text } from "@/pages/Settings/components";
import { useSettingValue } from "@/pages/Settings/utilities/hooks";

const profileIds = ["profile_1", "profile_2", "profile_3"] as const;

const OpenAIProfiles: FunctionComponent = () => {
  const activeProfile =
    useSettingValue<string>("settings-translator-openai_active_profile") ??
    "default";
  const defaultModel = useSettingValue<string>(
    "settings-translator-openai_model",
  );
  const profile1Name = useSettingValue<string>(
    "settings-translator-openai_profile_1_name",
  );
  const profile2Name = useSettingValue<string>(
    "settings-translator-openai_profile_2_name",
  );
  const profile3Name = useSettingValue<string>(
    "settings-translator-openai_profile_3_name",
  );

  const options = useMemo(() => {
    const names = [profile1Name, profile2Name, profile3Name];
    return [
      {
        value: "default",
        label: `Default${defaultModel ? ` (${defaultModel})` : ""}`,
      },
      ...profileIds.map((id, index) => ({
        value: id,
        label: names[index]?.trim() || `Profile ${index + 1} (empty)`,
      })),
    ];
  }, [defaultModel, profile1Name, profile2Name, profile3Name]);

  const prefix =
    activeProfile === "default"
      ? "settings-translator-openai"
      : `settings-translator-openai_${activeProfile}`;

  return (
    <Stack gap="xs">
      <Selector
        label="Active API profile"
        settingKey="settings-translator-openai_active_profile"
        options={options}
      />
      {activeProfile !== "default" && (
        <Text label="Profile name" settingKey={`${prefix}_name`} />
      )}
      <Text
        label="OpenAI-compatible base URL"
        settingKey={`${prefix}_base_url`}
      />
      <Text label="Model" settingKey={`${prefix}_model`} />
      <Password
        label="API key (optional for Ollama)"
        settingKey={`${prefix}_api_key`}
      />
      <Message>
        Select a saved profile to switch its endpoint, key, and model together.
        Changes take effect after Save.
      </Message>
    </Stack>
  );
};

export default OpenAIProfiles;
