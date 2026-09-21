/***************************************************************************
 *   Standalone "battle only" build of fheroes2.                           *
 *                                                                         *
 *   fheroes2 itself is distributed under the GNU General Public License   *
 *   version 2 or later; this file links against it and is covered by the  *
 *   same terms. See the LICENSE file in the fheroes2 source tree.         *
 ***************************************************************************/

// Entry point of the standalone "battle only" executable. It replaces the upstream entry point in
// src/fheroes2/game/fheroes2.cpp: instead of playing the intro videos and opening the main menu,
// this binary boots straight into the battle setup screen and keeps cycling between setup and
// combat, which is the workflow needed to drive individual battles without the adventure map.

#include <cstdlib>
#include <exception>
#include <memory>

// Managing compiler warnings for SDL headers
#if defined( __GNUC__ )
#pragma GCC diagnostic push

#pragma GCC diagnostic ignored "-Wdouble-promotion"
#pragma GCC diagnostic ignored "-Wold-style-cast"
#pragma GCC diagnostic ignored "-Wswitch-default"
#endif

#include <SDL_main.h> // IWYU pragma: keep

// Managing compiler warnings for SDL headers
#if defined( __GNUC__ )
#pragma GCC diagnostic pop
#endif

#if defined( _WIN32 )
#include <cassert>
#endif

#include <cstdio>
#include <cstring>
#include <string>

#include "battle_log.h"
#include "battle_only.h"
#include "battle_view.h"
#include "component_base.h"
#include "cursor.h"
#include "dialog.h"
#include "exception.h"
#include "external_ai.h"
#include "game.h"
#include "game_init.h"
#include "game_invalid_assets.h"
#include "game_mainmenu_ui.h"
#include "localevent.h"
#include "logging.h"
#include "scenario.h"
#include "settings.h"
#include "translations.h"
#include "ui_dialog.h"
#include "version.h"
#include "world.h"

namespace
{
    void printUsage( const char * program )
    {
        std::printf( "Usage: %s [--blue <mode>] [--red <mode>]\n"
                     "\n"
                     "Boots straight into the battle setup screen and loops between setup and combat.\n"
                     "\n"
                     "Modes, one per side (blue attacks, red defends):\n"
                     "  human                          played through the battle interface\n"
                     "  ai                             played by the AI that ships with fheroes2\n"
                     "  external:tcp://<host>:<port>   played by a harness listening on that address\n"
                     "\n"
                     "Defaults: --blue human --red ai. The mode of each side can also be changed in\n"
                     "the setup screen; a side can only be switched to the external AI there if an\n"
                     "endpoint for it was given here.\n"
                     "\n"
                     "  --scenario <file.json>         skip the setup screen: build both armies from\n"
                     "                                 the file, fight one battle, print a JSON result\n"
                     "                                 line on stdout and exit\n"
                     "  --headless                     do not draw the battle; for unattended runs\n"
                     "  --pause                        wait for a click before the first turn, with the\n"
                     "                                 battlefield already drawn - a pause screen for\n"
                     "                                 starting a screen recorder\n"
                     "  --reply-timeout <seconds>      how long to wait for one move from the external\n"
                     "                                 AI before giving up (default: 1800)\n"
                     "  --battle-log <file.jsonl>      record every choice both sides make, one JSON\n"
                     "                                 object per line, as the engine received it\n"
                     "\n"
                     "The harness is the server and the game connects to it. See battle/PROTOCOL.md.\n",
                     program );
    }

        // Set by --scenario. Empty means the interactive setup screen.
    std::string scenarioPath;

    // Set by --headless: fight without drawing anything, for unattended runs.
    bool headless{ false };

    // Set by --pause: wait for a click before the first turn, so a recording can be started.
    bool pauseBeforeStart{ false };

    // Returns false if the command line was not understood, after explaining why.
    bool parseCommandLine( char ** argv )
    {
        for ( int i = 1; argv[i] != nullptr; ++i ) {
            const char * const argument = argv[i];

            if ( std::strcmp( argument, "-h" ) == 0 || std::strcmp( argument, "--help" ) == 0 ) {
                printUsage( argv[0] );
                std::exit( EXIT_SUCCESS );
            }

            if ( std::strcmp( argument, "--version" ) == 0 ) {
                std::printf( "fheroes2 engine, version: %s\n", ENGINE_VERSION );
                std::exit( EXIT_SUCCESS );
            }

            if ( std::strcmp( argument, "--reply-timeout" ) == 0 ) {
                if ( argv[i + 1] == nullptr ) {
                    std::fprintf( stderr, "'--reply-timeout' needs a number of seconds.\n\n" );
                    printUsage( argv[0] );
                    return false;
                }

                ++i;

                const int seconds = std::atoi( argv[i] );
                if ( seconds <= 0 ) {
                    std::fprintf( stderr, "'--reply-timeout' needs a positive number of seconds, not '%s'.\n\n", argv[i] );
                    printUsage( argv[0] );
                    return false;
                }

                ExternalAI::setReplyTimeout( seconds );
                continue;
            }

            if ( std::strcmp( argument, "--battle-log" ) == 0 ) {
                if ( argv[i + 1] == nullptr ) {
                    std::fprintf( stderr, "'--battle-log' needs a file.\n\n" );
                    printUsage( argv[0] );
                    return false;
                }

                ++i;
                BattleLog::open( argv[i] );
                continue;
            }

            if ( std::strcmp( argument, "--headless" ) == 0 ) {
                headless = true;
                continue;
            }

            if ( std::strcmp( argument, "--pause" ) == 0 ) {
                pauseBeforeStart = true;
                continue;
            }

            if ( std::strcmp( argument, "--scenario" ) == 0 ) {
                if ( argv[i + 1] == nullptr ) {
                    std::fprintf( stderr, "'--scenario' needs a file.\n\n" );
                    printUsage( argv[0] );
                    return false;
                }

                ++i;
                scenarioPath = argv[i];
                continue;
            }

            int side = -1;
            if ( std::strcmp( argument, "--blue" ) == 0 ) {
                side = ExternalAI::attackerSide;
            }
            else if ( std::strcmp( argument, "--red" ) == 0 ) {
                side = ExternalAI::defenderSide;
            }

            if ( side < 0 ) {
                std::fprintf( stderr, "Unknown argument '%s'.\n\n", argument );
                printUsage( argv[0] );
                return false;
            }

            if ( argv[i + 1] == nullptr ) {
                std::fprintf( stderr, "'%s' needs a mode.\n\n", argument );
                printUsage( argv[0] );
                return false;
            }

            ++i;

            ExternalAI::ControlMode mode{ ExternalAI::ControlMode::HUMAN };
            std::string endpoint;
            std::string error;

            if ( !ExternalAI::parseModeSpec( argv[i], mode, endpoint, error ) ) {
                std::fprintf( stderr, "Cannot use '%s' as the mode for %s: %s\n\n", argv[i], argument, error.c_str() );
                printUsage( argv[0] );
                return false;
            }

            ExternalAI::setSideMode( side, mode, endpoint );
        }

        return true;
    }

    // Fights the battle described by --scenario and returns whether it ran. Nothing is drawn beyond
    // the battle itself: with both sides under non-human control this needs no input at all, which
    // is what lets a battle be scripted end to end.
    bool runScenario()
    {
        Settings & conf = Settings::Get();

        conf.SetGameType( Game::TYPE_BATTLEONLY );
        conf.setBattleAutoResolve( false );

        // A scenario has no human player by the engine's reckoning - a side played by the harness
        // is marked as AI-controlled - so without this the battle would be resolved invisibly.
        BattleView::setForceShow( !headless );
        BattleView::setPauseBeforeStart( pauseBeforeStart && !headless );

        fheroes2::drawMainMenuScreen();

        const CursorRestorer cursorRestorer( true, Cursor::POINTER );

        std::string error;

        try {
            if ( Scenario::run( scenarioPath, error ) ) {
                return true;
            }
        }
        catch ( const ExternalAI::ConnectionError & ex ) {
            error = ex.what();
        }

        // A broken scenario or an unreachable harness is a mistake in how the run was set up, so it
        // goes to stderr where a script will see it, not into a dialog nobody is watching.
        std::fprintf( stderr, "Scenario failed: %s\n", error.c_str() );
        ERROR_LOG( "Scenario failed: " << error )

        ExternalAI::endBattle();

        return false;
    }

    // Runs the battle setup screen, and on every confirmed setup the battle itself. Once a battle
    // ends, control returns to the setup screen with both armies restored as they were before the
    // fight, so a series of battles can be set up without restarting. Leaving the setup screen
    // through its Exit button terminates the loop.
    void runBattleOnlyLoop()
    {
        Settings & conf = Settings::Get();

        conf.SetGameType( Game::TYPE_BATTLEONLY );

        // "Auto resolve battles" is on by default and may also be set in the shared configuration
        // file. It would make Battle::Loader skip building the battle interface, which in turn makes
        // Battle::Arena force auto combat on every human-controlled side - the battle would play
        // itself out. This binary exists to play a single battle, so it always overrides the option.
        // Only the in-memory copy is changed; the configuration file is left alone.
        conf.setBattleAutoResolve( false );

        // In the interactive loop there is someone at the keyboard by definition, so the battle is
        // drawn even when neither side is played by a human.
        BattleView::setForceShow( !headless );
        BattleView::setPauseBeforeStart( pauseBeforeStart && !headless );

        // Both the setup screen and the battle are drawn on top of the main menu background.
        fheroes2::drawMainMenuScreen();

        const CursorRestorer cursorRestorer( true, Cursor::POINTER );

        Battle::Only battleOnlySetup;

        world.generateBattleOnlyMap( battleOnlySetup.terrainType() );

        // Nothing has been backed up yet, so on the first iteration this flag has no effect.
        bool allowBackup = true;
        bool resetBattleSetup = false;

        while ( battleOnlySetup.setup( allowBackup, resetBattleSetup ) ) {
            if ( resetBattleSetup ) {
                world.generateBattleOnlyMap( battleOnlySetup.terrainType() );
                battleOnlySetup.reset();

                resetBattleSetup = false;
                // The setup has just been wiped, so there is nothing worth restoring afterwards.
                allowBackup = false;

                continue;
            }

            world.setUniformTerrain( battleOnlySetup.terrainType() );

            try {
                battleOnlySetup.StartBattle();
            }
            catch ( const ExternalAI::ConnectionError & ex ) {
                // An unreachable harness is a setup mistake, so the battle is abandoned and the user
                // is put back in the setup screen to fix it. Carrying on without the harness would
                // mean the battle was played by something other than what was asked for.
                ERROR_LOG( "External AI connection failed: " << ex.what() )

                fheroes2::showStandardTextMessage( _( "External AI" ), std::string( _( "The battle was abandoned because the external AI could not be reached.\n\n" ) ) + ex.what(),
                                                   Dialog::OK );
            }

            ExternalAI::endBattle();

            // Bring both armies back the way they were, so the next battle can be set up from them.
            allowBackup = true;
        }
    }
}

int main( int argc, char ** argv )
{
// SDL2main.lib converts argv to UTF-8, but this application expects ANSI, use the original argv
#if defined( _WIN32 )
    assert( argc == __argc );

    argv = __argv;
#else
    (void)argc;
#endif

    if ( !parseCommandLine( argv ) ) {
        return EXIT_FAILURE;
    }

    try {
        auto hardwareComponent = Game::createHardwareComponent();

        Game::initLogging();
        Game::initDataDir();
        Game::initConfigDir( argv[0] );

        auto coreComponent = Game::createCoreComponent();
        auto displayComponent = Game::createDisplayComponent();
        auto dataComponent = Game::createDataComponent();
        auto audioComponent = Game::createAudioComponent( dataComponent.get() );

        Game::initPalette();
        Game::initTranslations();
        Game::initEventHandler();
        Game::initAnimation();
        Game::initHotKeys();

        // Game::initEventHandler() installs a hook that asks "Are you sure you want to quit?" when
        // the window is closed. That is right for the game and wrong here: this binary is driven by
        // a harness, and SDL turns the SIGTERM the harness sends into the very same close request -
        // so a harness shutting the game down would instead leave a dialog waiting for a click
        // nobody is there to give. Closing means closing.
        LocalEvent::Get().setQuitEventProcessingHook( []() { return true; } );

        try {
            if ( scenarioPath.empty() ) {
                runBattleOnlyLoop();
            }
            else if ( !runScenario() ) {
                return EXIT_FAILURE;
            }
        }
        catch ( const fheroes2::InvalidDataResources & ex ) {
            ERROR_LOG( ex.what() )
            showMissingAssetsImage();
            return EXIT_FAILURE;
        }
        catch ( const fheroes2::CorruptedExecutable & ex ) {
            ERROR_LOG( ex.what() )
            return EXIT_FAILURE;
        }
        catch ( const fheroes2::UserRequestedApplicationClosure & ) {
            // Yes, this an evil way of doing things but our application design doesn't allow to simply propagate application closure event.
            return EXIT_SUCCESS;
        }
    }
    catch ( const std::exception & ex ) {
        ERROR_LOG( "Exception '" << ex.what() << "' occurred during application runtime." )
        return EXIT_FAILURE;
    }
    catch ( ... ) {
        ERROR_LOG( "An unknown exception occurred during application runtime." )
        return EXIT_FAILURE;
    }

    return EXIT_SUCCESS;
}
