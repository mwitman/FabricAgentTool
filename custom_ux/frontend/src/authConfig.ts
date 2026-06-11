/**
 * MSAL configuration for Entra ID SSO authentication.
 *
 * Required Vite env vars (set in .env or .env.local):
 *   VITE_ENTRA_CLIENT_ID   — App registration client ID
 *   VITE_ENTRA_TENANT_ID   — Entra directory (tenant) ID
 */

import { Configuration, LogLevel } from "@azure/msal-browser";

const clientId = import.meta.env.VITE_ENTRA_CLIENT_ID as string;
const tenantId = import.meta.env.VITE_ENTRA_TENANT_ID as string;

if (!clientId) {
  console.error("VITE_ENTRA_CLIENT_ID is not set — SSO login will fail.");
}

export const msalConfig: Configuration = {
  auth: {
    clientId: clientId || "MISSING_CLIENT_ID",
    authority: `https://login.microsoftonline.com/${tenantId || "common"}`,
    redirectUri: window.location.origin,
    postLogoutRedirectUri: window.location.origin,
  },
  cache: {
    cacheLocation: "localStorage",
    storeAuthStateInCookie: false,
  },
  system: {
    loggerOptions: {
      logLevel: LogLevel.Warning,
      loggerCallback: (_level, message) => {
        console.debug(message);
      },
    },
  },
};

/** Scopes requested during login — profile info only. */
export const loginRequest = {
  scopes: ["openid", "profile", "offline_access"],
};

/** Scopes for acquiring a Fabric API access token. */
export const fabricTokenRequest = {
  scopes: ["https://api.fabric.microsoft.com/Workspace.Read.All", "https://api.fabric.microsoft.com/Item.ReadWrite.All"],
};

/** Scopes for executing DAX queries against Power BI/Fabric semantic models. */
export const powerBiTokenRequest = {
  scopes: ["https://analysis.windows.net/powerbi/api/Dataset.Read.All"],
};
