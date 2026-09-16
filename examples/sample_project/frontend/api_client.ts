/**
 * Frontend API client for the auth endpoints.
 *
 * IMPORTANT: parses the `user_id` field from the login response —
 * this is a consumer of the backend API contract in api/handlers.py.
 */

export interface LoginResponse {
  user_id: string;
  token: string;
}

export interface ProfileResponse {
  user_id: string;
  email: string;
  display_name: string;
}

export class ApiClient {
  private baseUrl: string;

  constructor(baseUrl: string) {
    this.baseUrl = baseUrl;
  }

  async login(email: string, password: string): Promise<LoginResponse> {
    const resp = await fetch(`${this.baseUrl}/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    if (!resp.ok) {
      throw new Error(`login failed: ${resp.status}`);
    }
    return resp.json() as Promise<LoginResponse>;
  }

  async me(token: string): Promise<ProfileResponse> {
    const resp = await fetch(`${this.baseUrl}/me`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!resp.ok) {
      throw new Error(`profile fetch failed: ${resp.status}`);
    }
    return resp.json() as Promise<ProfileResponse>;
  }

  storeSession(payload: LoginResponse): void {
    localStorage.setItem("session_token", payload.token);
    localStorage.setItem("session_user", payload.user_id);
  }
}
