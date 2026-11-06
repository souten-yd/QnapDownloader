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

# v0.1.54: what slows a first load on the NAS besides the ads above - tag managers and product analytics,
# session-replay / A-B testing scripts, more exchanges and trackers, social and share widgets, live-chat
# and web-push widgets. Still a static list in this file (nothing is downloaded); the same switch controls it
# (settings option block_ads and the per-site rule). CDNs, fonts, captcha / challenge hosts and link
# shorteners are deliberately absent.
EXTRA_DOMAINS = (
    # Tag managers / analytics / product analytics
    "adobedtm.com", "2o7.net", "ensighten.com", "tealiumiq.com", "segment.com", "segment.io", "mixpanel.com",
    "amplitude.com", "heapanalytics.com", "kissmetrics.com", "quantcast.com", "statcounter.com", "histats.com",
    "counter.yadro.ru", "top.mail.ru", "bat.r-ads.com", "sc-static.net", "snap.licdn.com", "px.ads.linkedin.com",
    "static.ads-twitter.com", "analytics.pinterest.com", "ct.pinterest.com", "pixel.wp.com", "stats.wp.com",
    "pixel.quantserve.com", "sb.scorecardresearch.com", "beacon.krxd.net", "cdn.mxpnl.com", "api.mixpanel.com",
    "clicky.com", "getclicky.com", "woopra.com", "gosquared.com", "matomo.cloud", "piwik.pro", "plausible.io",
    "umami.is", "simpleanalyticscdn.com", "usefathom.com", "statsig.com", "posthog.com", "rudderstack.com",
    "branch.io", "appsflyer.com", "adjust.com", "kochava.com", "singular.net", "braze.com", "appboy.com",
    "iterable.com", "klaviyo.com", "list-manage.com", "sendgrid.net", "customer.io",
    "newrelic.com", "bugsnag.com", "sentry-cdn.com", "browser.sentry-cdn.com", "rollbar.com", "datadoghq-browser-agent.com",
    "datadoghq.com", "trackjs.com", "raygun.io", "loggly.com", "bam.nr-data.net",
    # Session replay / A-B testing / personalisation
    "logrocket.com", "lr-ingest.io", "lrkt-in.com", "smartlook.com", "luckyorange.com", "inspectlet.com",
    "hotjar.io", "optimizely.com", "abtasty.com", "vwo.com", "visualwebsiteoptimizer.com", "bounceexchange.com",
    "convert.com", "dynamicyield.com", "evergage.com", "monetate.net", "qubitproducts.com", "crazyegg.net",
    "clicktale.net", "decibelinsight.net", "contentsquare.net", "contentsquare.com", "medallia.com",
    "usabilla.com", "survicate.com", "pendo.io", "walkme.com", "appcues.com",
    "userpilot.io", "whatfix.com", "heap.io",
    # More ad exchanges / SSPs / trackers
    "smaato.net", "bidr.io", "adgrx.com", "adhigh.net", "adnuntius.com", "ay.delivery", "bttrack.com",
    "connectad.io", "crwdcntrl.net", "cxense.com", "dotomi.com", "eqads.com", "flashtalking.com", "fwmrm.com",
    "ipredictive.com", "kargo.com", "lkqd.net", "media6degrees.com", "mookie1.com", "nexac.com", "onetag-sys.com",
    "pippio.com", "pubmine.com", "rfihub.com", "rhythmone.com", "sitescout.com", "sonobi.com", "turn.com",
    "videohub.tv", "yieldlab.net", "yldbt.com", "zemanta.com", "adblade.com", "adbrite.com", "admixer.net",
    "adtech.de", "adtechus.com", "advertserve.com", "bannerflow.com", "btloader.com", "cpmstar.com",
    "ezoic.net", "ezodn.com", "gadsme.com", "adpushup.com", "monetizemore.com", "snigelweb.com", "pbstck.com",
    "ads.pubmatic.com", "adtelligent.com", "vidazoo.com", "vidoomy.com", "viewable.com",
    "teads.com", "seedtag.com", "nativo.com", "nativo.net", "sharethrough.com", "ads.yimg.com",
    "advertising.yahoo.com", "i-mobile.co.jp", "microad.jp", "microad.net", "fout.jp", "adingo.jp",
    "adlantis.jp", "impact-ad.jp", "nend.net", "popin.cc", "logly.co.jp", "fluct.jp", "ad-stir.com", "adstir.com",
    "gssprt.jp", "yjtag.yahoo.co.jp", "ads.google.com", "adservice.google.co.jp", "ad.doubleclick.net",
    "ad.atdmt.com", "atdmt.com", "serving-sys.net", "tribalfusion.com", "yieldmanager.com", "overture.com",
    "ads.twitter.com", "ads-api.twitter.com", "analytics.twitter.com", "ads.reddit.com", "events.redditmedia.com",
    "pixel.rubiconproject.com", "pixel.adsafeprotected.com",
    # Social / share widgets and comment systems (heavy third-party scripts)
    "facebook.net", "platform.twitter.com", "syndication.twitter.com", "platform.linkedin.com",
    "assets.pinterest.com", "widgets.pinterest.com", "platform.instagram.com", "staticxx.facebook.com",
    "sharethis.com", "addthis.com", "addtoany.com", "po.st", "shareaholic.com", "sumo.com", "sumome.com",
    "disqus.com", "disquscdn.com", "livefyre.com", "spot.im", "viafoura.com", "vuukle.com", "b.hatena.ne.jp", "s.hatena.ne.jp", "widgets.getpocket.com", "buttons.reddit.com",
    "platform.tumblr.com",
    # Live chat / support / web-push widgets
    "intercom.io", "intercomcdn.com", "drift.com", "driftt.com", "tawk.to", "crisp.chat", "zopim.com",
    "livechatinc.com", "olark.com", "hs-scripts.com", "hs-analytics.net", "hs-banner.com", "usemessages.com", "freshchat.com", "tidio.co",
    "tidiochat.com", "smartsuppchat.com", "jivosite.com", "onesignal.com",
    "pushwoosh.com", "webpushr.com", "izooto.com", "pushcrew.com", "subscribers.com", "pushnami.com",
    "aimtell.com", "pushassist.com", "cleverpush.com", "notix.io", "gravitec.net", "feedify.net",
)


def _clean(value):
    return value.strip().lower()


def _hoster_related(domain):
    return any(domain == h or domain.endswith("." + h) or h.endswith("." + domain)
               for h in NEVER_BLOCK)


def domains():
    """Unique, lower-case, hoster-free domains in list order."""
    seen, result = set(), []
    for item in DOMAINS + EXTRA_DOMAINS:
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
