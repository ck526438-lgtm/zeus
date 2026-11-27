//+------------------------------------------------------------------+
//| ZeusNewsExporter.mq5                                              |
//| Exporte le calendrier économique natif de MT5 (CalendarValueHistory)|
//| vers Common\Files\zeus_news.csv, lu par Zeus HFT (Python), qui     |
//| suspend les entrées 15 min avant / 15 min après chaque annonce.    |
//|                                                                    |
//| Installation : copier ce fichier dans MQL5\Experts\, le compiler  |
//| (F7 dans MetaEditor), puis le faire glisser sur n'importe quel     |
//| graphique. Il ne passe aucun ordre.                                |
//+------------------------------------------------------------------+
#property copyright   "Zeus HFT"
#property version     "1.00"
#property description "Exporte les annonces économiques à fort impact pour Zeus HFT (aucun ordre)."

input string InpCurrencies      = "USD";            // Devises suivies (séparées par des virgules)
input bool   InpIncludeModerate = false;            // Inclure aussi l'impact moyen
input int    InpDaysAhead       = 7;                // Jours à venir exportés
input int    InpRefreshMinutes  = 15;               // Fréquence de mise à jour
input string InpFileName        = "zeus_news.csv";  // Fichier dans Common\Files

//+------------------------------------------------------------------+
int OnInit()
  {
   Export();
   EventSetTimer(MathMax(1, InpRefreshMinutes) * 60);
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason) { EventKillTimer(); }
void OnTimer()                  { Export(); }
void OnTick()                   { }

//+------------------------------------------------------------------+
//| Décalage heure serveur - UTC, arrondi à la demi-heure             |
//+------------------------------------------------------------------+
long ServerUtcOffset()
  {
   long raw = (long)(TimeTradeServer() - TimeGMT());
   return (long)(MathRound(raw / 1800.0) * 1800.0);
  }

//+------------------------------------------------------------------+
void Export()
  {
   long     offset = ServerUtcOffset();
   datetime now    = TimeTradeServer();
   datetime from   = now - 86400;
   datetime to     = now + InpDaysAhead * 86400;

   int h = FileOpen(InpFileName, FILE_WRITE | FILE_CSV | FILE_COMMON | FILE_ANSI, ';');
   if(h == INVALID_HANDLE)
     {
      PrintFormat("Zeus : impossible d'écrire %s (erreur %d)", InpFileName, GetLastError());
      return;
     }
   FileWrite(h, "time_utc", "currency", "importance", "event");

   string currencies[];
   int n = StringSplit(InpCurrencies, ',', currencies);
   int count = 0;
   for(int c = 0; c < n; c++)
     {
      string ccy = currencies[c];
      StringTrimLeft(ccy);
      StringTrimRight(ccy);
      if(ccy == "")
         continue;
      MqlCalendarValue values[];
      if(CalendarValueHistory(values, from, to, NULL, ccy) <= 0)
         continue;
      for(int i = 0; i < ArraySize(values); i++)
        {
         MqlCalendarEvent ev;
         if(!CalendarEventById(values[i].event_id, ev))
            continue;
         bool high = (ev.importance == CALENDAR_IMPORTANCE_HIGH);
         bool moderate = (ev.importance == CALENDAR_IMPORTANCE_MODERATE);
         if(!high && !(InpIncludeModerate && moderate))
            continue;
         datetime utc = (datetime)((long)values[i].time - offset);
         string name = ev.name;
         StringReplace(name, ";", ",");
         FileWrite(h, TimeToString(utc, TIME_DATE | TIME_MINUTES), ccy,
                   high ? "high" : "moderate", name);
         count++;
        }
     }
   FileClose(h);
   PrintFormat("Zeus : %d annonces exportées vers Common\\Files\\%s", count, InpFileName);
  }
//+------------------------------------------------------------------+
