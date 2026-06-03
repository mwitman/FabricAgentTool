import { Configuration, LogLevel } from "@azure/msal-browser";

const clientId = import.meta.env.VITE_ENTRA_CLIENT_ID as string;
const tenantId = import.meta.env.VITE_ENTRA_TENANT_ID as string;

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
      loggerCallback: (_level, message) => console.debug(message),
    },
  },
};

export const loginRequest = { scopes: ["openid", "profile", "offline_access"] };
export const fabricTokenRequest = { scopes: ["https://api.fabric.microsoft.com/Workspace.Read.All", "https://api.fabric.microsoft.com/Item.ReadWrite.All"] };
export const powerBiTokenRequest = { scopes: ["https://analysis.windows.net/powerbi/api/Dataset.Read.All"] };
