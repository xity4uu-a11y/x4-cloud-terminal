//+------------------------------------------------------------------+
//|                                          X4_CloudBridge_EA.mq5   |
//|                  X4 Cloud Terminal — HMAC-SHA256 HTTPS Bridge EA |
//|                                                                  |
//| Connects ANY MetaTrader 5 terminal (Windows, Linux+Wine Docker,  |
//| or Broker VPS) to a remote X4 Cloud Terminal instance over HTTPS |
//| without opening inbound ports, public IPs, or Azure Windows VMs. |
//|                                                                  |
//| Setup in MetaTrader 5:                                           |
//|   1. Tools -> Options -> Expert Advisors                         |
//|   2. Enable "Allow Algorithmic Trading"                          |
//|   3. Enable "Allow WebRequest for listed URL"                    |
//|   4. Add your X4 Cloud URL (e.g. https://your-app.onrender.com)  |
//|   5. Attach this EA to any chart and set InpBridgeSecret         |
//+------------------------------------------------------------------+
#property copyright "X4 Cloud Terminal Contributors (MIT)"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>

input string InpCloudBaseUrl     = "http://127.0.0.1:8501";
input string InpBridgeSecret     = "change-me-generate-a-different-32-byte-secret";
input string InpSymbolsCsv       = "XAUUSD,EURUSD,GBPUSD,USDJPY";
input int    InpSyncIntervalSec  = 5;
input long   InpMagicNumber      = 240400;
input int    InpMaxSlippagePts   = 30;
input bool   InpAllowRealAccount = false;   // FAIL-CLOSED: Demo accounts only by default!

CTrade g_trade;
string g_symbols[];

//+------------------------------------------------------------------+
//| Utility: Convert bytes to lowercase hex string                   |
//+------------------------------------------------------------------+
string BytesToHex(const uchar &data[])
{
   string hex = "";
   int len = ArraySize(data);
   for(int i = 0; i < len; i++)
      hex += StringFormat("%02x", data[i]);
   return hex;
}

//+------------------------------------------------------------------+
//| Compute HMAC-SHA256 using native MQL5 CryptEncode(SHA-256)       |
//+------------------------------------------------------------------+
string ComputeHmacSha256Hex(const string secret, const string message)
{
   uchar keyBytes[];
   int keyLen = StringToCharArray(secret, keyBytes, 0, StringLen(secret), CP_UTF8);
   ArrayResize(keyBytes, keyLen);

   uchar blockKey[64];
   ArrayInitialize(blockKey, 0);

   if(keyLen > 64)
   {
      uchar hashedKey[];
      uchar emptyKey[];
      CryptEncode(CRYPT_HASH_SHA256, keyBytes, emptyKey, hashedKey);
      for(int i = 0; i < ArraySize(hashedKey) && i < 64; i++)
         blockKey[i] = hashedKey[i];
   }
   else
   {
      for(int i = 0; i < keyLen; i++)
         blockKey[i] = keyBytes[i];
   }

   uchar oKeyPad[64], iKeyPad[64];
   for(int i = 0; i < 64; i++)
   {
      oKeyPad[i] = (uchar)(blockKey[i] ^ 0x5c);
      iKeyPad[i] = (uchar)(blockKey[i] ^ 0x36);
   }

   uchar msgBytes[];
   int msgLen = StringToCharArray(message, msgBytes, 0, StringLen(message), CP_UTF8);
   ArrayResize(msgBytes, msgLen);

   uchar innerData[];
   ArrayResize(innerData, 64 + msgLen);
   ArrayCopy(innerData, iKeyPad, 0, 0, 64);
   if(msgLen > 0)
      ArrayCopy(innerData, msgBytes, 64, 0, msgLen);

   uchar innerHash[];
   uchar emptyKey[];
   CryptEncode(CRYPT_HASH_SHA256, innerData, emptyKey, innerHash);

   uchar outerData[];
   ArrayResize(outerData, 64 + ArraySize(innerHash));
   ArrayCopy(outerData, oKeyPad, 0, 0, 64);
   ArrayCopy(outerData, innerHash, 64, 0, ArraySize(innerHash));

   uchar finalHash[];
   CryptEncode(CRYPT_HASH_SHA256, outerData, emptyKey, finalHash);
   return BytesToHex(finalHash);
}

//+------------------------------------------------------------------+
//| Minimal JSON helpers                                             |
//+------------------------------------------------------------------+
string JsonEscape(const string s)
{
   string out = s;
   StringReplace(out, "\\", "\\\\");
   StringReplace(out, "\"", "\\\"");
   return out;
}

string ExtractJsonString(const string json, const string key, int startPos = 0)
{
   string needle = "\"" + key + "\"";
   int kpos = StringFind(json, needle, startPos);
   if(kpos < 0) return "";
   int colon = StringFind(json, ":", kpos + StringLen(needle));
   if(colon < 0) return "";
   int q1 = StringFind(json, "\"", colon + 1);
   if(q1 < 0) return "";
   int q2 = StringFind(json, "\"", q1 + 1);
   if(q2 < 0) return "";
   return StringSubstr(json, q1 + 1, q2 - q1 - 1);
}

double ExtractJsonNumber(const string json, const string key, int startPos = 0)
{
   string needle = "\"" + key + "\"";
   int kpos = StringFind(json, needle, startPos);
   if(kpos < 0) return 0.0;
   int colon = StringFind(json, ":", kpos + StringLen(needle));
   if(colon < 0) return 0.0;
   int p = colon + 1;
   int n = StringLen(json);
   while(p < n && (StringGetCharacter(json, p) == ' ' || StringGetCharacter(json, p) == '\t')) p++;
   int end = p;
   while(end < n)
   {
      ushort ch = StringGetCharacter(json, end);
      if((ch >= '0' && ch <= '9') || ch == '.' || ch == '-' || ch == '+')
         end++;
      else
         break;
   }
   return StringToDouble(StringSubstr(json, p, end - p));
}

//+------------------------------------------------------------------+
//| Signed HTTP POST helper                                          |
//+------------------------------------------------------------------+
bool SignedPost(const string path, const string bodyJson, string &responseText)
{
   string url = InpCloudBaseUrl + path;
   string ts = IntegerToString((long)TimeGMT());
   string sig = ComputeHmacSha256Hex(InpBridgeSecret, ts + "." + bodyJson);

   string headers = "Content-Type: application/json\r\n"
                  + "X-X4-Timestamp: " + ts + "\r\n"
                  + "X-X4-Signature: " + sig + "\r\n";

   uchar postData[];
   int bodyLen = StringToCharArray(bodyJson, postData, 0, StringLen(bodyJson), CP_UTF8);
   ArrayResize(postData, bodyLen);

   uchar result[];
   string resHeaders = "";
   int status = WebRequest("POST", url, headers, 6000, postData, result, resHeaders);
   if(status < 0)
   {
      PrintFormat("[X4 Bridge] WebRequest failed (%s) err=%d. Add URL in MT5 Options -> Expert Advisors.", url, GetLastError());
      return false;
   }
   responseText = CharArrayToString(result, 0, ArraySize(result), CP_UTF8);
   if(status < 200 || status >= 300)
   {
      PrintFormat("[X4 Bridge] HTTP %d from %s: %s", status, path, responseText);
      return false;
   }
   return true;
}

//+------------------------------------------------------------------+
//| Build Sync JSON Payload                                          |
//+------------------------------------------------------------------+
string BuildSyncPayload()
{
   ENUM_ACCOUNT_TRADE_MODE modeEnum = (ENUM_ACCOUNT_TRADE_MODE)AccountInfoInteger(ACCOUNT_TRADE_MODE);
   string modeStr = "unknown";
   if(modeEnum == ACCOUNT_TRADE_MODE_DEMO) modeStr = "demo";
   else if(modeEnum == ACCOUNT_TRADE_MODE_CONTEST) modeStr = "demo";
   else if(modeEnum == ACCOUNT_TRADE_MODE_REAL) modeStr = "real";

   string json = "{";
   json += "\"agent_version\":\"X4_CloudBridge_EA/1.0\",";
   json += "\"account\":{";
   json += "\"login\":" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) + ",";
   json += "\"server\":\"" + JsonEscape(AccountInfoString(ACCOUNT_SERVER)) + "\",";
   json += "\"trade_mode\":\"" + modeStr + "\",";
   json += "\"balance\":" + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2) + ",";
   json += "\"equity\":" + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2) + ",";
   json += "\"margin\":" + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN), 2) + ",";
   json += "\"margin_free\":" + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2) + ",";
   json += "\"currency\":\"" + JsonEscape(AccountInfoString(ACCOUNT_CURRENCY)) + "\"";
   json += "},\"quotes\":{";

   int qCount = 0;
   for(int i = 0; i < ArraySize(g_symbols); i++)
   {
      string sym = g_symbols[i];
      if(sym == "") continue;
      SymbolSelect(sym, true);
      MqlTick tick;
      if(!SymbolInfoTick(sym, tick) || tick.bid <= 0 || tick.ask <= 0) continue;
      if(qCount > 0) json += ",";
      int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      json += "\"" + JsonEscape(sym) + "\":{\"bid\":" + DoubleToString(tick.bid, digits)
            + ",\"ask\":" + DoubleToString(tick.ask, digits) + "}";
      qCount++;
   }

   json += "},\"positions\":[";
   int pCount = 0;
   int totalPos = PositionsTotal();
   for(int i = 0; i < totalPos; i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket)) continue;
      if(pCount > 0) json += ",";
      string sym = PositionGetString(POSITION_SYMBOL);
      int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      string side = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? "BUY" : "SELL";
      json += "{";
      json += "\"ticket\":" + IntegerToString((long)ticket) + ",";
      json += "\"symbol\":\"" + JsonEscape(sym) + "\",";
      json += "\"side\":\"" + side + "\",";
      json += "\"volume\":" + DoubleToString(PositionGetDouble(POSITION_VOLUME), 2) + ",";
      json += "\"open_price\":" + DoubleToString(PositionGetDouble(POSITION_PRICE_OPEN), digits) + ",";
      json += "\"current_price\":" + DoubleToString(PositionGetDouble(POSITION_PRICE_CURRENT), digits) + ",";
      json += "\"sl\":" + DoubleToString(PositionGetDouble(POSITION_SL), digits) + ",";
      json += "\"tp\":" + DoubleToString(PositionGetDouble(POSITION_TP), digits) + ",";
      json += "\"profit\":" + DoubleToString(PositionGetDouble(POSITION_PROFIT), 2) + ",";
      json += "\"magic\":" + IntegerToString(PositionGetInteger(POSITION_MAGIC)) + ",";
      json += "\"comment\":\"" + JsonEscape(PositionGetString(POSITION_COMMENT)) + "\"";
      json += "}";
      pCount++;
   }
   json += "]}";
   return json;
}

//+------------------------------------------------------------------+
//| Execute queued commands returned by /api/bridge/sync             |
//+------------------------------------------------------------------+
void ProcessCommands(const string responseJson)
{
   int pos = StringFind(responseJson, "\"commands\"");
   if(pos < 0) return;

   while(true)
   {
      int cmdPos = StringFind(responseJson, "\"command_id\"", pos);
      if(cmdPos < 0) break;

      string cmdId  = ExtractJsonString(responseJson, "command_id", cmdPos);
      string action = ExtractJsonString(responseJson, "action", cmdPos);
      pos = cmdPos + 12;

      if(cmdId == "" || action == "") continue;

      ENUM_ACCOUNT_TRADE_MODE modeEnum = (ENUM_ACCOUNT_TRADE_MODE)AccountInfoInteger(ACCOUNT_TRADE_MODE);
      if(modeEnum == ACCOUNT_TRADE_MODE_REAL && !InpAllowRealAccount)
      {
         string rej = "{\"command_id\":\"" + JsonEscape(cmdId) + "\",\"status\":\"REJECTED\",\"message\":\"REAL_LOCKED_IN_EA\"}";
         string tmp;
         SignedPost("/api/bridge/ack", rej, tmp);
         continue;
      }

      if(action == "OPEN_MARKET")
      {
         string sym  = ExtractJsonString(responseJson, "symbol", cmdPos);
         string side = ExtractJsonString(responseJson, "side", cmdPos);
         double vol  = ExtractJsonNumber(responseJson, "volume", cmdPos);
         double sl   = ExtractJsonNumber(responseJson, "sl", cmdPos);
         double tp   = ExtractJsonNumber(responseJson, "tp", cmdPos);
         string cmt  = ExtractJsonString(responseJson, "comment", cmdPos);

         SymbolSelect(sym, true);
         g_trade.SetExpertMagicNumber(InpMagicNumber);
         g_trade.SetDeviationInPoints(InpMaxSlippagePts);

         bool ok = false;
         if(side == "BUY")
            ok = g_trade.Buy(vol, sym, 0.0, sl, tp, cmt);
         else if(side == "SELL")
            ok = g_trade.Sell(vol, sym, 0.0, sl, tp, cmt);

         string status = ok ? "ACKED" : "REJECTED";
         string ack = "{\"command_id\":\"" + JsonEscape(cmdId) + "\",\"status\":\"" + status
                    + "\",\"broker_ticket\":" + IntegerToString((long)g_trade.ResultOrder())
                    + ",\"retcode\":" + IntegerToString((long)g_trade.ResultRetcode())
                    + ",\"message\":\"" + JsonEscape(g_trade.ResultRetcodeDescription()) + "\"}";
         string tmp;
         SignedPost("/api/bridge/ack", ack, tmp);
      }
      else if(action == "CLOSE_POSITION")
      {
         ulong ticket = (ulong)ExtractJsonNumber(responseJson, "ticket", cmdPos);
         bool ok = g_trade.PositionClose(ticket, InpMaxSlippagePts);
         string status = ok ? "ACKED" : "REJECTED";
         string ack = "{\"command_id\":\"" + JsonEscape(cmdId) + "\",\"status\":\"" + status
                    + "\",\"broker_ticket\":" + IntegerToString((long)ticket)
                    + ",\"retcode\":" + IntegerToString((long)g_trade.ResultRetcode())
                    + ",\"message\":\"" + JsonEscape(g_trade.ResultRetcodeDescription()) + "\"}";
         string tmp;
         SignedPost("/api/bridge/ack", ack, tmp);
      }
   }
}

//+------------------------------------------------------------------+
//| Lifecycle                                                        |
//+------------------------------------------------------------------+
int OnInit()
{
   StringSplit(InpSymbolsCsv, ',', g_symbols);
   for(int i = 0; i < ArraySize(g_symbols); i++)
      StringTrimLeft(g_symbols[i]), StringTrimRight(g_symbols[i]);

   EventSetTimer(MathMax(2, InpSyncIntervalSec));
   PrintFormat("[X4 Bridge] Initialized | target=%s | real_allowed=%s", InpCloudBaseUrl, InpAllowRealAccount ? "true" : "false");
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}

void OnTimer()
{
   string payload = BuildSyncPayload();
   string resp = "";
   if(SignedPost("/api/bridge/sync", payload, resp))
      ProcessCommands(resp);
}
//+------------------------------------------------------------------+
