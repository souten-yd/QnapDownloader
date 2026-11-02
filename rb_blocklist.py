"""Ad / tracker / popunder block list for the NAS browser (v0.1.24).

Kept in the Hub (not in the container image) so it can change without a new
image. Applied per tab and per probe target with Network.setBlockedURLs, which
only cancels matching requests inside Chromium; nothing here fetches anything.

Link shorteners / redirectors (adf.ly and similar) are deliberately absent:
the long-press probe follows redirect chains and needs them. Entries are domains; both "*://<domain>/*" and "*://*.<domain>/*"
are generated. Hoster domains (and anything that would match them) are never
blocked: patterns() drops such entries and a unit test asserts none exist.
"""

# File hosters whose pages and downloads must always load / be recognised.
NEVER_BLOCK = ("rapidgator.net", "rg.to")

DOMAINS = (
    # Google advertising / measurement
    "doubleclick.net", "googlesyndication.com", "googleadservices.com",
    "adservice.google.com", "google-analytics.com", "googletagmanager.com",
    "googletagservices.com", "2mdn.net", "admob.com", "adsense.com",
    "pagead2.googlesyndication.com", "analytics.google.com",
    # Popunder / push / pop networks common on file-sharing blogs
    "popads.net", "popcash.net", "propellerads.com", "exoclick.com", "exosrv.com",
    "realsrv.com", "magsrv.com", "juicyads.com", "jads.co", "adsterra.com",
    "trafficjunky.net", "trafficjunky.com", "hilltopads.net", "hilltopads.com",
    "a-ads.com", "ad-maven.com", "admaven.com", "clickadu.com", "adcash.com",
    "onclickads.net", "onclasrv.com", "oclasrv.com", "monetag.com", "popmyads.com",
    "trafficstars.com", "tsyndicate.com", "ero-advertising.com", "clickaine.com",
    "adspyglass.com", "bidvertiser.com", "clicksor.com", "adxpansion.com",
    "trafficfactory.biz", "trafmag.com", "tubecorporate.com", "adtng.com",
    "pushame.com", "pushengage.com", "richads.com", "evadav.com", "adk2x.com",
    "acscdn.com", "popunderjs.com", "popundertotal.com", "yllix.com",
    "mellowads.com", "adnium.com", "plugrush.com", "adskeeper.com",
    "adrecover.com", "pubfuture.com", "pubfutureads.com",
    # Programmatic exchanges / SSPs / DSPs
    "adnxs.com", "criteo.com", "criteo.net", "taboola.com", "outbrain.com",
    "mgid.com", "revcontent.com", "zedo.com", "rubiconproject.com", "pubmatic.com",
    "openx.net", "adform.net", "smartadserver.com", "casalemedia.com",
    "indexexchange.com", "advertising.com", "yieldmo.com", "33across.com",
    "sharethrough.com", "triplelift.com", "teads.tv", "bidswitch.net",
    "rtbhouse.com", "adroll.com", "amazon-adsystem.com", "adsrvr.org",
    "contextweb.com", "lijit.com", "sovrn.com", "gumgum.com", "spotxchange.com",
    "springserve.com", "stickyadstv.com", "tremorhub.com", "undertone.com",
    "mediavine.com", "adthrive.com", "infolinks.com", "media.net",
    "serving-sys.com", "adition.com", "adscale.de", "improvedigital.com",
    "conversantmedia.com", "emxdgt.com", "openx.com", "appnexus.com",
    "inmobi.com", "mopub.com", "unityads.unity3d.com", "applovin.com",
    # Measurement / tracking / data brokers
    "scorecardresearch.com", "quantserve.com", "quantcount.com", "hotjar.com",
    "moatads.com", "doubleverify.com", "adsafeprotected.com", "demdex.net",
    "krxd.net", "bluekai.com", "exelator.com", "rlcdn.com", "agkn.com",
    "mathtag.com", "tapad.com", "everesttech.net", "omtrdc.net", "chartbeat.com",
    "chartbeat.net", "newrelic.com", "nr-data.net", "mxpnl.com", "crazyegg.com",
    "mouseflow.com", "fullstory.com", "clarity.ms", "bat.bing.com", "ads.yahoo.com",
    "analytics.yahoo.com", "ads.linkedin.com", "ads-twitter.com", "ads.tiktok.com",
    "analytics.tiktok.com", "pixel.facebook.com", "adsymptotic.com", "id5-sync.com",
    "liadm.com", "permutive.com", "owneriq.net", "tiqcdn.com", "imrworldwide.com",
    "an.yandex.ru", "mc.yandex.ru",
)


def _clean(value):
    return value.strip().lower()


def _hoster_related(domain):
    return any(domain == h or domain.endswith("." + h) or h.endswith("." + domain)
               for h in NEVER_BLOCK)


def domains():
    """Unique, lower-case, hoster-free domains in list order."""
    seen, result = set(), []
    for item in DOMAINS:
        domain = _clean(item)
        if not domain or domain in seen or _hoster_related(domain):
            continue
        seen.add(domain)
        result.append(domain)
    return tuple(result)


def patterns():
    """Network.setBlockedURLs patterns: the apex and every subdomain of each entry."""
    result = []
    for domain in domains():
        result.append("*://%s/*" % domain)
        result.append("*://*.%s/*" % domain)
    return tuple(result)
