import BaseApi from "./base";

export interface ManualSearchOptions {
  keyword?: string;
  providers?: string[];
}

function searchPath(
  path: string,
  idKey: string,
  id: number,
  options?: ManualSearchOptions,
) {
  const params = new URLSearchParams({ [idKey]: String(id) });
  const keyword = options?.keyword?.trim();
  if (keyword) params.set("keyword", keyword);
  options?.providers?.forEach((provider) =>
    params.append("providers", provider),
  );
  return `${path}?${params.toString()}`;
}

class ProviderApi extends BaseApi {
  constructor() {
    super("/providers");
  }

  async providers(history = false) {
    const response = await this.get<DataWrapper<System.Provider[]>>("", {
      history,
    });
    return response.data;
  }

  async reset() {
    await this.post("", { action: "reset" });
  }

  async movies(id: number, options?: ManualSearchOptions) {
    const response = await this.get<DataWrapper<SearchResultType[]>>(
      searchPath("/movies", "radarrid", id, options),
    );
    return response.data;
  }

  async downloadMovieSubtitle(radarrid: number, form: FormType.ManualDownload) {
    await this.post("/movies", form, { radarrid });
  }

  async clearMovieRejection(radarrid: number, subtitle: string) {
    await this.delete("/movies", { radarrid, subtitle });
  }

  async episodes(episodeid: number, options?: ManualSearchOptions) {
    const response = await this.get<DataWrapper<SearchResultType[]>>(
      searchPath("/episodes", "episodeid", episodeid, options),
    );
    return response.data;
  }

  async downloadEpisodeSubtitle(
    seriesid: number,
    episodeid: number,
    form: FormType.ManualDownload,
  ) {
    await this.post("/episodes", form, { seriesid, episodeid });
  }

  async clearEpisodeRejection(episodeid: number, subtitle: string) {
    await this.delete("/episodes", { episodeid, subtitle });
  }

  async previewEpisodeSeasonReplacement(episodeid: number, subtitle: string) {
    return this.get<SeasonReplacementPreview>("/episodes/season", {
      episodeid,
      subtitle,
    });
  }

  async replaceEpisodeSeasonSubtitles(
    episodeid: number,
    form: Pick<
      FormType.ManualDownload,
      "subtitle" | "hi" | "forced" | "original_format"
    >,
  ) {
    const response = await this.post<SeasonReplacementQueued>(
      "/episodes/season",
      { episodeid, ...form },
    );
    return response.data;
  }
}

const providerApi = new ProviderApi();
export default providerApi;
