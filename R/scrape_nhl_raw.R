## Scrape raw NHL game JSON and schedules into fastRhockey-nhl-raw
## Season year = the larger year of the season (e.g. 2025-26 → 2026)
## Usage:
##   Rscript R/scrape_nhl_raw.R -s 2026           (single season, 2025-26)
##   Rscript R/scrape_nhl_raw.R -s 2024 -e 2026   (range: 2023-24 through 2025-26)
##   Rscript R/scrape_nhl_raw.R -s 2026 -r TRUE   (rescrape existing)
##
## Outputs:
##   nhl/json/raw/{game_id}.json    — raw API data organized under old-format keys
##   nhl/json/final/{game_id}.json  — fully processed via fastRhockey pipeline
##   nhl/schedules/{rds,parquet}/nhl_schedule_{year}.*
##     (includes game_json, game_json_url pointing to final/)
##   nhl/nhl_schedule_master.{rds,parquet}

suppressPackageStartupMessages(library(fastRhockey))
suppressPackageStartupMessages(library(dplyr))
suppressPackageStartupMessages(library(glue))
suppressPackageStartupMessages(library(purrr))
suppressPackageStartupMessages(library(furrr))
suppressPackageStartupMessages(library(future))
suppressPackageStartupMessages(library(jsonlite))
suppressPackageStartupMessages(library(httr))
suppressPackageStartupMessages(library(arrow))
suppressPackageStartupMessages(library(optparse))
suppressPackageStartupMessages(library(cli))


option_list <- list(
  optparse::make_option(
    c("-s", "--start_year"),
    action = "store",
    default = fastRhockey:::most_recent_nhl_season(),
    type = "integer",
    help = "Start year of the seasons to process [default: current season]"
  ),
  optparse::make_option(
    c("-e", "--end_year"),
    action = "store",
    default = NA_integer_,
    type = "integer",
    help = "End year of the seasons to process [default: same as start_year]"
  ),
  optparse::make_option(
    c("-r", "--rescrape"),
    action = "store",
    default = TRUE,
    type = "logical",
    help = "Rescrape games that already have JSON files [default: TRUE]"
  )
)

opt <- optparse::parse_args(optparse::OptionParser(option_list = option_list))
options(stringsAsFactors = FALSE)
options(scipen = 999)

if (is.na(opt$end_year)) opt$end_year <- opt$start_year
season_vector <- opt$start_year:opt$end_year
rescrape <- opt$rescrape

# ── Logging ──────────────────────────────────────────────────────────────
LOG_FILE <- glue::glue("logs/fastRhockey_nhl_raw_logfile_{opt$start_year}.log")
logging <- function(msg, level = "INFO") {
  entry <- paste0(format(Sys.time(), "[%Y-%m-%d %H:%M:%S] "), level, ": ", msg)
  cat(entry, "\n", file = LOG_FILE, append = FALSE)
}
logging("=== NHL Raw Scraper started ===")


RAW_REPO <- "sportsdataverse/fastRhockey-nhl-raw"
RAW_BRANCH <- "main"
PATH_RAW <- "nhl/json/raw"
PATH_FINAL <- "nhl/json/final"


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════

.safe_get_json <- function(url) {
  tryCatch(
    {
      res <- httr::RETRY("GET", url, times = 3, pause_min = 1)
      httr::stop_for_status(res)
      jsonlite::fromJSON(
        httr::content(res, as = "text", encoding = "UTF-8"),
        flatten = FALSE
      )
    },
    error = function(e) NULL
  )
}

.extract_default <- function(x) {
  if (is.null(x)) {
    return(NA_character_)
  }
  if (is.list(x) && !is.null(x[["default"]])) {
    return(x[["default"]])
  }
  as.character(x)
}

.write_json <- function(data, path) {
  jsonlite::write_json(data,
    path = path,
    auto_unbox = TRUE, null = "null", na = "null"
  )
}


# ═══════════════════════════════════════════════════════════════════════
# build_raw_json: fetch 4 API endpoints, organize under old-format keys
#
# Mirrors the old fastRhockey-data nhl/json/*.json structure:
#   all_plays     → raw plays array from play-by-play endpoint
#   player_box    → raw playerByGameStats from boxscore endpoint
#   linescore     → assembled from right-rail + landing
#   decisions     → three stars + goalie W/L
#   scratches     → from right-rail gameInfo
#   team_coaches  → from right-rail gameInfo
#   scoring       → from landing summary
#   penalties     → from landing summary
#
# Plus raw endpoint responses for anything that doesn't map cleanly.
# ═══════════════════════════════════════════════════════════════════════

build_raw_json <- function(gid) {
  pbp_raw <- .safe_get_json(
    glue("https://api-web.nhle.com/v1/gamecenter/{gid}/play-by-play")
  )
  box_raw <- .safe_get_json(
    glue("https://api-web.nhle.com/v1/gamecenter/{gid}/boxscore")
  )
  landing <- .safe_get_json(
    glue("https://api-web.nhle.com/v1/gamecenter/{gid}/landing")
  )
  rail <- .safe_get_json(
    glue("https://api-web.nhle.com/v1/gamecenter/{gid}/right-rail")
  )

  if (is.null(pbp_raw)) {
    return(NULL)
  }

  # purrr::pluck() safely navigates nested lists/data frames from fromJSON
  # without the $ operator issues. Returns .default (NULL) on any missing path.

  # ── Core data ──────────────────────────────────────────────────────
  all_plays <- purrr::pluck(pbp_raw, "plays")
  rosters <- purrr::pluck(pbp_raw, "rosterSpots")
  player_box <- purrr::pluck(box_raw, "playerByGameStats")
  team_box <- purrr::pluck(rail, "teamGameStats")

  game_info <- list(
    id           = purrr::pluck(pbp_raw, "id"),
    season       = purrr::pluck(pbp_raw, "season"),
    gameType     = purrr::pluck(pbp_raw, "gameType"),
    gameDate     = purrr::pluck(pbp_raw, "gameDate"),
    venue        = purrr::pluck(pbp_raw, "venue"),
    gameState    = purrr::pluck(pbp_raw, "gameState"),
    startTimeUTC = purrr::pluck(pbp_raw, "startTimeUTC"),
    homeTeam     = purrr::pluck(pbp_raw, "homeTeam"),
    awayTeam     = purrr::pluck(pbp_raw, "awayTeam")
  )

  # ── Linescore (right-rail + landing) ───────────────────────────────
  linescore <- purrr::pluck(rail, "linescore") %>% as.list()
  if (!is.null(linescore)) {
    linescore[["shotsByPeriod"]] <- purrr::pluck(rail, "shotsByPeriod")
    linescore[["teamGameStats"]] <- purrr::pluck(rail, "teamGameStats")
    linescore[["clock"]] <- purrr::pluck(landing, "clock")
    linescore[["periodDescriptor"]] <- purrr::pluck(landing, "periodDescriptor")
    linescore[["teams"]] <- list(
      home = list(
        team = list(
          id           = purrr::pluck(landing, "homeTeam", "id"),
          name         = .extract_default(purrr::pluck(landing, "homeTeam", "commonName")),
          abbreviation = purrr::pluck(landing, "homeTeam", "abbrev")
        ),
        goals = purrr::pluck(landing, "homeTeam", "score"),
        shotsOnGoal = purrr::pluck(landing, "homeTeam", "sog")
      ),
      away = list(
        team = list(
          id           = purrr::pluck(landing, "awayTeam", "id"),
          name         = .extract_default(purrr::pluck(landing, "awayTeam", "commonName")),
          abbreviation = purrr::pluck(landing, "awayTeam", "abbrev")
        ),
        goals = purrr::pluck(landing, "awayTeam", "score"),
        shotsOnGoal = purrr::pluck(landing, "awayTeam", "sog")
      )
    )
  }

  # ── Decisions (three stars + goalie W/L) ───────────────────────────
  decisions <- NULL
  stars <- purrr::pluck(landing, "summary", "threeStars")
  if (!is.null(stars)) {
    winner_id <- NA
    winner_name <- NA
    loser_id <- NA
    loser_name <- NA

    # Extract goalie decisions — "name" is a nested data.frame column
    # with {default, cs, fi, sk} sub-columns, so extract default names first
    for (side in c("awayTeam", "homeTeam")) {
      gl <- purrr::pluck(box_raw, "playerByGameStats", side, "goalies")
      if (is.null(gl)) next
      gl_df <- if (is.data.frame(gl)) gl else dplyr::bind_rows(gl)
      if (nrow(gl_df) == 0 || !"decision" %in% names(gl_df)) next
      # Extract goalie names: name column may be data.frame or character
      name_col <- gl_df[["name"]]
      goalie_names <- if (is.data.frame(name_col)) {
        name_col[["default"]]
      } else {
        purrr::map_chr(name_col, .extract_default)
      }
      for (i in seq_len(nrow(gl_df))) {
        dec <- gl_df[["decision"]][i]
        if (is.null(dec) || is.na(dec)) next
        pid <- gl_df[["playerId"]][i]
        nm <- goalie_names[i]
        if (dec == "W") {
          winner_id <- pid
          winner_name <- nm
        }
        if (dec == "L") {
          loser_id <- pid
          loser_name <- nm
        }
      }
    }

    decisions <- list(
      threeStars = stars,
      winner     = list(id = winner_id, name = winner_name),
      loser      = list(id = loser_id, name = loser_name)
    )
  }

  # ── Scratches (right-rail gameInfo) ────────────────────────────────
  # firstName/lastName come back as nested {default, cs, de, ...} objects
  # from the API. When fromJSON parses them, "firstName" becomes a nested
  # data.frame column. We extract the "default" sub-column for names.
  .parse_scratches <- function(sc) {
    if (is.null(sc) || length(sc) == 0) {
      return(NULL)
    }
    if (is.data.frame(sc)) {
      ids <- sc[["id"]]
      # firstName may be a data.frame with a "default" column, or a character vector
      fn_raw <- sc[["firstName"]]
      ln_raw <- sc[["lastName"]]
      first_names <- if (is.data.frame(fn_raw)) fn_raw[["default"]] else purrr::map_chr(fn_raw, .extract_default)
      last_names <- if (is.data.frame(ln_raw)) ln_raw[["default"]] else purrr::map_chr(ln_raw, .extract_default)
      purrr::pmap(
        list(id = ids, firstName = first_names, lastName = last_names),
        function(id, firstName, lastName) list(id = id, firstName = firstName, lastName = lastName)
      )
    } else {
      purrr::map(sc, function(s) {
        list(
          id        = purrr::pluck(s, "id"),
          firstName = .extract_default(purrr::pluck(s, "firstName")),
          lastName  = .extract_default(purrr::pluck(s, "lastName"))
        )
      })
    }
  }

  scratches <- purrr::map(c("awayTeam", "homeTeam"), function(side) {
    .parse_scratches(purrr::pluck(rail, "gameInfo", side, "scratches"))
  }) %>%
    purrr::compact() %>%
    purrr::list_flatten()
  if (length(scratches) == 0) scratches <- NULL

  # ── Team coaches (right-rail gameInfo) ──────────────────────────────
  team_coaches <- purrr::imap(
    list(awayTeam = "Away", homeTeam = "Home"),
    function(side_label, side_key) {
      hc <- purrr::pluck(rail, "gameInfo", side_key, "headCoach")
      if (is.null(hc)) {
        return(NULL)
      }
      list(name = .extract_default(hc), home_away = side_label)
    }
  ) %>% purrr::compact()
  if (length(team_coaches) == 0) team_coaches <- NULL

  # ── Scoring / penalties (landing summary) ──────────────────────────
  scoring <- purrr::pluck(landing, "summary", "scoring")
  penalties <- purrr::pluck(landing, "summary", "penalties")

  # ── Assemble ───────────────────────────────────────────────────────
  list(
    # Old-format keys (raw/unprocessed data)
    all_plays = all_plays,
    game_info = game_info,
    rosters = rosters,
    team_box = team_box,
    player_box = player_box,
    linescore = linescore,
    decisions = decisions,
    scratches = scratches,
    team_coaches = team_coaches,
    scoring = scoring,
    penalties = penalties,

    # Full raw API responses
    pbp_raw = pbp_raw,
    boxscore_raw = box_raw,
    landing_raw = landing,
    right_rail_raw = rail
  )
}


# ═══════════════════════════════════════════════════════════════════════
# build_final_json: run full fastRhockey processing pipeline
# ═══════════════════════════════════════════════════════════════════════

build_final_json <- function(gid, raw_data = NULL) {
  # Start from the raw JSON structure if provided, otherwise build it
  if (is.null(raw_data)) {
    raw_data <- build_raw_json(gid)
  }
  if (is.null(raw_data)) {
    return(NULL)
  }

  # Copy all raw keys as the base
  final <- raw_data

  # ── Overlay processed PBP (replaces raw all_plays with enriched version) ──
  tryCatch(
    {
      feed <- fastRhockey::nhl_game_feed(game_id = gid, include_shifts = TRUE)
      final$all_plays <- feed$pbp # processed PBP with coords, shifts, xG
      final$game_info <- feed$game_info # parsed game metadata tibble
      final$rosters <- feed$rosters # parsed rosters tibble
    },
    error = function(e) {
      cli::cli_alert_warning("PBP pipeline failed for {gid}: {conditionMessage(e)}")
    }
  )

  # ── Add parsed boxscore stats (new keys on top of raw player_box) ──
  tryCatch(
    {
      box <- fastRhockey::nhl_game_boxscore(game_id = gid)
      final$team_box_parsed <- box$team_box # 2-row team totals tibble
      final$skater_stats <- box$skater_stats # per-skater tibble
      final$goalie_stats <- box$goalie_stats # per-goalie tibble
    },
    error = function(e) {
      cli::cli_alert_warning("Boxscore parsing failed for {gid}: {conditionMessage(e)}")
    }
  )

  final
}


# ═══════════════════════════════════════════════════════════════════════
# download_game: raw + final
# ═══════════════════════════════════════════════════════════════════════

download_game <- function(gid, process = TRUE,
                          path_raw = "nhl/json/raw",
                          path_final = "nhl/json/final") {
  # Step 1: Build and save raw JSON
  raw_data <- tryCatch(
    build_raw_json(gid),
    error = function(e) {
      cli::cli_alert_warning("Raw JSON failed for {gid}: {conditionMessage(e)}")
      NULL
    }
  )
  if (!is.null(raw_data)) {
    .write_json(raw_data, glue("{path_raw}/{gid}.json"))
  }

  # Step 2: Build and save final JSON (raw structure + processed overlays)
  if (process) {
    final_data <- tryCatch(
      build_final_json(gid, raw_data = raw_data),
      error = function(e) {
        cli::cli_alert_warning("Final JSON failed for {gid}: {conditionMessage(e)}")
        NULL
      }
    )
    if (!is.null(final_data)) {
      .write_json(final_data, glue("{path_final}/{gid}.json"))
    }
  }
}


# ═══════════════════════════════════════════════════════════════════════
# Main loop
# ═══════════════════════════════════════════════════════════════════════

for (season_year in season_vector) {
  season_label <- paste0(
    season_year - 1, "-",
    substr(as.character(season_year), 3, 4)
  )
  cli::cli_h1("Processing {season_label} season")
  logging(glue("=== {season_label} season ==="))


  # ── STEP 1: Fetch and save schedule ──────────────────────────────────

  cli::cli_progress_step(
    msg = "Fetching {season_label} schedule",
    msg_done = "Fetched {season_label} schedule"
  )

  sched <- fastRhockey::nhl_schedule(season = season_year) %>%
    dplyr::tibble() %>%
    dplyr::mutate(season = season_year)

  for (d in c("nhl/schedules/rds", "nhl/schedules/parquet")) {
    if (!dir.exists(d)) dir.create(d, recursive = TRUE)
  }

  games <- dplyr::filter(sched, game_state == "OFF")
  cli::cli_alert_info("{nrow(games)} completed games in schedule")
  logging(glue("{nrow(games)} completed games in schedule"))

  if (nrow(games) == 0) {
    cli::cli_alert_warning("No completed games. Skipping season.")
    saveRDS(sched, glue("nhl/schedules/rds/nhl_schedule_{season_year}.rds"))
    arrow::write_parquet(sched,
      glue("nhl/schedules/parquet/nhl_schedule_{season_year}.parquet"),
      compression = "gzip"
    )
    next
  }


  # ── STEP 2: Scrape raw + process final game JSON ────────────────────

  for (d in c(PATH_RAW, PATH_FINAL)) {
    if (!dir.exists(d)) dir.create(d, recursive = TRUE)
  }

  if (rescrape) {
    games_to_scrape <- games
  } else {
    existing_final <- as.integer(gsub("\\.json$", "", list.files(PATH_FINAL)))
    games_to_scrape <- dplyr::filter(games, !(game_id %in% existing_final))
  }

  cli::cli_progress_step(
    msg = "Scraping {nrow(games_to_scrape)} games for {season_label}",
    msg_done = "Scraped {nrow(games_to_scrape)} games for {season_label}"
  )

  if (nrow(games_to_scrape) > 0) {
    n_games <- nrow(games_to_scrape)
    for (i in seq_len(n_games)) {
      gid <- games_to_scrape$game_id[i]
      tryCatch(
        {
          download_game(gid,
            process = TRUE,
            path_raw = PATH_RAW, path_final = PATH_FINAL
          )
        },
        error = function(e) {
          cli::cli_alert_warning("Failed game {gid}: {conditionMessage(e)}")
        }
      )
      if (i %% 50 == 0 || i == n_games) {
        cli::cli_alert_info("  Progress: {i}/{n_games} games")
      }
    }
  }


  # ── STEP 3: Update schedule with game_json + game_json_url ───────────

  cli::cli_progress_step(
    msg = "Updating {season_label} schedule with JSON links",
    msg_done = "Updated {season_label} schedule with JSON links"
  )

  final_files <- as.integer(gsub("\\.json$", "", list.files(PATH_FINAL)))

  sched <- sched %>%
    dplyr::mutate(
      game_json = game_id %in% final_files,
      game_json_url = dplyr::if_else(
        game_json,
        glue::glue(
          "https://raw.githubusercontent.com/{RAW_REPO}/{RAW_BRANCH}/{PATH_FINAL}/{game_id}.json"
        ),
        NA_character_
      )
    )

  saveRDS(sched, glue("nhl/schedules/rds/nhl_schedule_{season_year}.rds"))
  arrow::write_parquet(sched,
    glue("nhl/schedules/parquet/nhl_schedule_{season_year}.parquet"),
    compression = "gzip"
  )

  n_raw <- length(list.files(PATH_RAW, pattern = "\\.json$"))
  n_final <- length(list.files(PATH_FINAL, pattern = "\\.json$"))
  cli::cli_alert_success(
    "{sum(sched$game_json)} of {nrow(sched)} games linked ({n_raw} raw, {n_final} final)"
  )
  logging(glue("{sum(sched$game_json)} of {nrow(sched)} games linked ({n_raw} raw, {n_final} final)"))

  rm(games, games_to_scrape, sched)
  gc()
} # end for season_year


# ═══════════════════════════════════════════════════════════════════════
# Build cross-season master schedule
# ═══════════════════════════════════════════════════════════════════════

cli::cli_progress_step(
  msg = "Building master schedule",
  msg_done = "Master schedule built"
)

sched_files <- list.files("nhl/schedules/rds", pattern = "\\.rds$", full.names = TRUE)
sched_all <- purrr::map_dfr(sched_files, readRDS) %>%
  dplyr::arrange(dplyr::desc(game_date))

saveRDS(sched_all, "nhl/nhl_schedule_master.rds")
arrow::write_parquet(sched_all, "nhl/nhl_schedule_master.parquet", compression = "gzip")

cli::cli_alert_success(
  "{nrow(sched_all)} total schedule rows, {sum(sched_all$game_json, na.rm = TRUE)} with final JSON"
)
logging(glue("Master: {nrow(sched_all)} schedule rows, {sum(sched_all$game_json, na.rm = TRUE)} with final JSON"))
logging("=== NHL Raw Scraper complete ===")
cli::cli_h1("All done!")
