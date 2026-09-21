/***************************************************************************
 *   Standalone fheroes2 "battle only" build.                              *
 *                                                                         *
 *   External AI: lets a side of a battle be played by a separate process   *
 *   (the harness), which is where an LLM is actually called.               *
 *                                                                         *
 *   The harness is the server and outlives the game: it listens, the game  *
 *   dials out to it. That way an LLM session can stay warm across many     *
 *   battles and the game can be restarted freely.                          *
 *                                                                         *
 *   Wire format is newline-delimited JSON, one message per line, described *
 *   in battle/PROTOCOL.md.                                                 *
 *                                                                         *
 *   Note on failure handling: an externally controlled side is never       *
 *   handed over to the game AI. Doing so would look exactly like the side  *
 *   playing well on its own, which is the one outcome that must not be     *
 *   mistaken for the harness working. Failures are surfaced instead.       *
 ***************************************************************************/

#pragma once

#include <stdexcept>
#include <string>

#include "color.h"

namespace Battle
{
    class Arena;
    class Unit;
    class Actions;
}

namespace ExternalAI
{
    // Thrown when the harness cannot be reached or the connection breaks mid-battle. The battle
    // loop catches it, abandons the battle and returns to the setup screen with a message: an
    // unreachable harness is a setup mistake, and continuing the battle without it would be
    // misleading.
    class ConnectionError : public std::runtime_error
    {
    public:
        explicit ConnectionError( const std::string & what )
            : std::runtime_error( what )
        {}
    };

    // Who plays a given side of the battle.
    enum class ControlMode
    {
        // Played through the normal battle interface.
        HUMAN,
        // Played by the AI that ships with fheroes2.
        GAME_AI,
        // Played by the harness over the wire.
        EXTERNAL
    };

    // Sides are identified the way the setup screen orders them: 0 is the attacker (blue), 1 is the
    // defender (red).
    inline constexpr int attackerSide{ 0 };
    inline constexpr int defenderSide{ 1 };

    // Parses "human", "ai", or "external:tcp://<host>:<port>" into a mode and an endpoint. Returns
    // false and sets 'error' if the spec is not understood.
    bool parseModeSpec( const std::string & spec, ControlMode & mode, std::string & endpoint, std::string & error );

    void setSideMode( const int side, const ControlMode mode, const std::string & endpoint );

    // How long to wait for a single reply before giving up on the harness. The default is 1800
    // seconds: a local reasoning model can spend many minutes on one move, and a timeout that fires
    // while the model is still working looks like a broken harness when nothing is wrong.
    void setReplyTimeout( const int seconds );

    ControlMode sideMode( const int side );

    // Cycles a side through the modes that are actually available: a side can only be switched to
    // EXTERNAL if an endpoint was configured for it on the command line.
    void cycleSideMode( const int side );

    // Short label for the setup screen, e.g. "Human", "Game AI", "External AI".
    const char * sideModeName( const int side );

    bool hasEndpoint( const int side );

    // Maps a player color onto a side. Called once per battle, before it starts.
    void bindColorToSide( const PlayerColor color, const int side );

    bool isExternalColor( const PlayerColor color );

    // Who is playing the side of this colour: "Human", "Game AI" or "External AI". For telling the
    // user what they are about to watch.
    const char * controllerName( const PlayerColor color );

    // Plays one turn of 'currentUnit' by asking the harness for an action and translating it into
    // commands.
    //
    // If the harness answers with something illegal, it is told why and asked again a bounded
    // number of times; if it still cannot produce a legal action, the unit skips its turn. A skip
    // is deterministic, visible in the battle log, and cannot be mistaken for competent play.
    //
    // Throws ConnectionError if the harness cannot be reached or goes away.
    void takeTurn( Battle::Arena & arena, const Battle::Unit & currentUnit, Battle::Actions & actions );

    // Closes any open connections, so the next battle starts from a clean handshake.
    void endBattle();
}
