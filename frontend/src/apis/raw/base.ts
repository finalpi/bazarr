import { AxiosResponse } from "axios";
import client from "./client";
import { createFormData } from "./formdata";

class BaseApi {
  prefix: string;

  constructor(prefix: string) {
    this.prefix = prefix;
  }

  protected async get<T = unknown>(path: string, params?: LooseObject) {
    const response = await client.axios.get<T>(this.prefix + path, { params });
    return response.data;
  }

  protected post<T = void>(
    path: string,
    formdata?: LooseObject,
    params?: LooseObject,
  ): Promise<AxiosResponse<T>> {
    const form = createFormData(formdata);
    return client.axios.post(this.prefix + path, form, {
      params,
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
    });
  }

  protected patch<T = void>(
    path: string,
    formdata?: LooseObject,
    params?: LooseObject,
  ): Promise<AxiosResponse<T>> {
    const form = createFormData(formdata);
    return client.axios.patch(this.prefix + path, form, { params });
  }

  protected delete<T = void>(
    path: string,
    formdata?: LooseObject,
    params?: LooseObject,
  ): Promise<AxiosResponse<T>> {
    const form = createFormData(formdata);
    return client.axios.delete(this.prefix + path, { params, data: form });
  }
}

export default BaseApi;
