/***************************************************************************
 *   Standalone fheroes2 "battle only" build.                              *
 *                                                                         *
 *   Implementation of the external AI transport and protocol. See          *
 *   external_ai.h for the design notes and battle/PROTOCOL.md for the      *
 *   message shapes.                                                        *
 ***************************************************************************/

#include "external_ai.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <cerrno>
#include <cstring>
#include <map>
#include <memory>
#include <string>
#include <vector>

#include <future>
#include <thread>

#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>

#include "army_troop.h"
#include "battle_arena.h"
#include "battle_army.h"
#include "battle_board.h"
#include "battle_cell.h"
#include "battle_command.h"
#include "battle_interface.h"
#include "battle_troop.h"
#include "heroes.h"
#include "heroes_base.h"
#include "json.h"
#include "localevent.h"
#include "math_base.h"
#include "screen.h"
#include "translations.h"
#include "ui_text.h"
#include "logging.h"
#include "monster.h"
#include "monster_info.h"
#include "spell.h"
#include "spell_info.h"
#include "spell_storage.h"

namespace
{
    // How many times a harness may answer with an illegal action before the unit simply skips its
    // turn. Enough to let a model correct itself from the error text, few enough that a confused
    // harness cannot stall the battle indefinitely.
    constexpr int maxIllegalActionRetries{ 3 };

    // Guards against a harness that accepts the connection but never answers. Generous by default,
    // because the harness is calling an LLM behind the scenes and a slow model is not an error: a
    // local reasoning model can spend minutes on a single move.
    constexpr int defaultReplyTimeoutSeconds{ 1800 };

    int replyTimeoutSeconds{ defaultReplyTimeoutSeconds };

    constexpr int protocolVersion{ 1 };

    struct SideConfig
    {
        ExternalAI::ControlMode mode{ ExternalAI::ControlMode::HUMAN };
        std::string endpoint;
    };

    // How often the waiting screen is redrawn. Frequent enough to feel alive, rare enough not to
    // spend a core on an animation nobody is interacting with.
    constexpr int uiFrameIntervalMs{ 50 };

    // A line-oriented TCP connection to the harness.
    class Connection
    {
    public:
        explicit Connection( const std::string & endpoint )
            : _endpoint( endpoint )
        {}

        Connection( const Connection & ) = delete;
        Connection & operator=( const Connection & ) = delete;

        ~Connection()
        {
            close();
        }

        void close()
        {
            if ( _fd >= 0 ) {
                ::close( _fd );
                _fd = -1;
            }
        }

        bool isOpen() const
        {
            return _fd >= 0;
        }

        // Throws ExternalAI::ConnectionError if the harness cannot be reached.
        void connect( const std::string & host, const std::string & port )
        {
            addrinfo hints{};
            hints.ai_family = AF_UNSPEC;
            hints.ai_socktype = SOCK_STREAM;

            addrinfo * results = nullptr;

            const int status = ::getaddrinfo( host.c_str(), port.c_str(), &hints, &results );
            if ( status != 0 ) {
                throw ExternalAI::ConnectionError( "cannot resolve '" + host + ":" + port + "': " + gai_strerror( status ) );
            }

            const std::unique_ptr<addrinfo, decltype( &::freeaddrinfo )> resultsGuard( results, &::freeaddrinfo );

            std::string lastError{ "no addresses returned" };

            for ( const addrinfo * it = results; it != nullptr; it = it->ai_next ) {
                const int fd = ::socket( it->ai_family, it->ai_socktype, it->ai_protocol );
                if ( fd < 0 ) {
                    lastError = std::strerror( errno );
                    continue;
                }

                if ( ::connect( fd, it->ai_addr, it->ai_addrlen ) == 0 ) {
                    _fd = fd;

                    // Turn requests are small and latency matters more than packing them together.
                    const int one = 1;
                    ::setsockopt( _fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof( one ) );

                    timeval timeout{};
                    timeout.tv_sec = replyTimeoutSeconds;
                    ::setsockopt( _fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof( timeout ) );
                    ::setsockopt( _fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof( timeout ) );

                    return;
                }

                lastError = std::strerror( errno );
                ::close( fd );
            }

            throw ExternalAI::ConnectionError( "cannot connect to the harness at " + _endpoint + ": " + lastError );
        }

        void sendLine( const std::string & line )
        {
            const std::string payload = line + "\n";

            size_t sent = 0;
            while ( sent < payload.size() ) {
                const ssize_t written = ::send( _fd, payload.data() + sent, payload.size() - sent, MSG_NOSIGNAL );

                if ( written <= 0 ) {
                    if ( written < 0 && errno == EINTR ) {
                        continue;
                    }

                    const std::string reason = ( written < 0 ) ? std::strerror( errno ) : "connection closed";
                    close();
                    throw ExternalAI::ConnectionError( "cannot send to the harness at " + _endpoint + ": " + reason );
                }

                sent += static_cast<size_t>( written );
            }
        }

        // Blocking. Called only from the worker thread in awaitReply(), never from the thread that
        // draws the game.
        std::string receiveLine()
        {
            for ( ;; ) {
                const size_t newline = _buffer.find( '\n' );
                if ( newline != std::string::npos ) {
                    std::string line = _buffer.substr( 0, newline );
                    _buffer.erase( 0, newline + 1 );

                    if ( !line.empty() && line.back() == '\r' ) {
                        line.pop_back();
                    }

                    return line;
                }

                std::array<char, 8192> chunk{};
                const ssize_t received = ::recv( _fd, chunk.data(), chunk.size(), 0 );

                if ( received > 0 ) {
                    _buffer.append( chunk.data(), static_cast<size_t>( received ) );
                    continue;
                }

                if ( received < 0 && errno == EINTR ) {
                    continue;
                }

                std::string reason;
                if ( received == 0 ) {
                    reason = "connection closed by the harness";
                }
                else if ( errno == EAGAIN || errno == EWOULDBLOCK ) {
                    reason = "no reply within " + std::to_string( replyTimeoutSeconds ) + " seconds";
                }
                else {
                    reason = std::strerror( errno );
                }

                close();
                throw ExternalAI::ConnectionError( "cannot read from the harness at " + _endpoint + ": " + reason );
            }
        }

        const std::string & endpoint() const
        {
            return _endpoint;
        }

    private:
        std::string _endpoint;
        std::string _buffer;
        int _fd{ -1 };
    };

    // Sends a request and waits for the reply on a worker thread, while this thread keeps the game
    // drawing and servicing input.
    //
    // The engine's turn loop is synchronous: AI::BattlePlanner::BattleTurn has to return with the
    // actions filled in, so the battle genuinely cannot proceed until the harness answers. What can
    // be avoided is freezing the whole application while that happens, which is what a blocking
    // read on this thread would do. On a local reasoning model the wait is minutes, long enough for
    // the window manager to declare the window unresponsive.
    //
    // Only the worker touches the socket, and this thread reads the result only after the future is
    // ready, so no locking is needed. Nothing in the arena is touched from the worker.
    std::string awaitReply( Connection & connection, const std::string & request, const std::string & waitingFor )
    {
        std::future<std::string> reply = std::async( std::launch::async, [&connection, &request]() {
            connection.sendLine( request );

            return connection.receiveLine();
        } );

        fheroes2::Display & display = fheroes2::Display::instance();

        // Put the battlefield on screen before settling in to wait. The engine only redraws it as
        // part of playing an action, so on the very first request of a battle - and after any
        // dialog - what is on screen may be whatever was there before the battle started.
        if ( Battle::Interface * ui = Battle::Arena::GetInterface(); ui != nullptr ) {
            ui->Redraw();
            display.render();
        }

        // A strip along the bottom of the screen, wide enough for the longest message.
        const fheroes2::Rect area{ 0, display.height() - 24, display.width(), 24 };

        // The battlefield underneath is static while we wait, so one copy is enough to wipe the
        // previous frame's text before drawing the next.
        fheroes2::Image background( area.width, area.height );
        fheroes2::Copy( display, area.x, area.y, background, 0, 0, area.width, area.height );

        const auto started = std::chrono::steady_clock::now();

        while ( reply.wait_for( std::chrono::milliseconds( uiFrameIntervalMs ) ) != std::future_status::ready ) {
            // allowExit is false: the arena is half-way through a turn, so quitting from here would
            // leave it inconsistent. The engine's own paths handle closing the game.
            LocalEvent::Get().HandleEvents( false, false );

            const auto seconds = std::chrono::duration_cast<std::chrono::seconds>( std::chrono::steady_clock::now() - started ).count();

            std::string message = waitingFor;
            message += " (";
            message += std::to_string( seconds );
            message += "s)";

            fheroes2::Copy( background, 0, 0, display, area.x, area.y, area.width, area.height );

            const fheroes2::Text text( message, fheroes2::FontType::normalWhite() );
            text.draw( area.x + ( area.width - text.width() ) / 2, area.y + 4, display );

            display.render( area );
        }

        fheroes2::Copy( background, 0, 0, display, area.x, area.y, area.width, area.height );
        display.render( area );

        // Rethrows whatever the worker threw, on this thread, where it can be handled.
        return reply.get();
    }

    std::array<SideConfig, 2> sideConfigs;
    std::map<PlayerColor, int> colorToSide;
    std::map<int, std::unique_ptr<Connection>> connections;

    bool isValidSide( const int side )
    {
        return side == ExternalAI::attackerSide || side == ExternalAI::defenderSide;
    }

    const char * sideName( const int side )
    {
        return ( side == ExternalAI::attackerSide ) ? "attacker" : "defender";
    }

    const char * colorName( const PlayerColor color )
    {
        switch ( color ) {
        case PlayerColor::BLUE:
            return "blue";
        case PlayerColor::RED:
            return "red";
        case PlayerColor::GREEN:
            return "green";
        case PlayerColor::YELLOW:
            return "yellow";
        case PlayerColor::ORANGE:
            return "orange";
        case PlayerColor::PURPLE:
            return "purple";
        default:
            return "none";
        }
    }

    // Splits "tcp://host:port" into its parts. IPv6 literals are written in brackets, as in a URL:
    // tcp://[::1]:9000.
    bool parseTcpEndpoint( const std::string & endpoint, std::string & host, std::string & port, std::string & error )
    {
        constexpr const char * prefix = "tcp://";
        constexpr size_t prefixLength = 6;

        if ( endpoint.compare( 0, prefixLength, prefix ) != 0 ) {
            error = "expected an endpoint of the form tcp://<host>:<port>";
            return false;
        }

        const std::string authority = endpoint.substr( prefixLength );
        if ( authority.empty() ) {
            error = "the endpoint has no host";
            return false;
        }

        size_t separator = std::string::npos;

        if ( authority.front() == '[' ) {
            const size_t closing = authority.find( ']' );
            if ( closing == std::string::npos ) {
                error = "unterminated IPv6 address in the endpoint";
                return false;
            }

            host = authority.substr( 1, closing - 1 );
            separator = authority.find( ':', closing );
        }
        else {
            separator = authority.rfind( ':' );
            if ( separator != std::string::npos ) {
                host = authority.substr( 0, separator );
            }
        }

        if ( separator == std::string::npos ) {
            error = "the endpoint has no port";
            return false;
        }

        port = authority.substr( separator + 1 );

        if ( host.empty() || port.empty() ) {
            error = "the endpoint needs both a host and a port";
            return false;
        }

        return true;
    }

    void writeUnit( Json::Writer & writer, const Battle::Unit & unit, const bool isCurrent )
    {
        writer.startObject();
        writer.value( "uid", static_cast<int64_t>( unit.GetUID() ) );
        writer.value( "name", unit.GetName() );
        writer.value( "color", colorName( unit.GetArmyColor() ) );
        writer.value( "count", static_cast<int64_t>( unit.GetCount() ) );
        writer.value( "head", static_cast<int64_t>( unit.GetHeadIndex() ) );

        if ( unit.isWide() ) {
            writer.value( "tail", static_cast<int64_t>( unit.GetTailIndex() ) );
        }

        writer.value( "hit_points", static_cast<int64_t>( unit.GetHitPoints() ) );
        writer.value( "hit_points_left", static_cast<int64_t>( unit.GetHitPointsLeft() ) );
        writer.value( "attack", static_cast<int64_t>( unit.GetAttack() ) );
        writer.value( "defense", static_cast<int64_t>( unit.GetDefense() ) );

        // Base damage and hit points of a SINGLE creature. Without these the damage a move would do
        // cannot be worked out at all, which leaves anything reading this ranking its options by
        // feel - and a reader that cannot compare two options tends not to settle on either.
        //
        // Monster:: is explicit on all three: Troop::GetDamageMin() and friends already multiply by
        // the stack size, and Unit::GetHitPoints() reports the whole stack. Sending those would be
        // silently wrong - the reader multiplies by the count itself, so the count would be applied
        // twice and every figure it computed would be off by that factor.
        writer.value( "damage_min", static_cast<int64_t>( unit.Monster::GetDamageMin() ) );
        writer.value( "damage_max", static_cast<int64_t>( unit.Monster::GetDamageMax() ) );
        writer.value( "hit_points_each", static_cast<int64_t>( unit.Monster::GetHitPoints() ) );
        writer.value( "speed", static_cast<int64_t>( unit.GetSpeed() ) );
        writer.value( "is_wide", unit.isWide() );
        writer.value( "is_flying", unit.isFlying() );
        writer.value( "is_archer", unit.isArchers() );
        writer.value( "shots", static_cast<int64_t>( unit.GetShots() ) );
        writer.value( "is_current", isCurrent );

        // What this creature type can do beyond move and hit. These are fixed properties of the
        // type, and without them a reader cannot know that a Mage ignores the melee penalty or
        // that a Dwarf resists magic - it would reason about the stack as if it were ordinary.
        // ignoreBasicAbilities skips the ones every creature has, which would only be noise.
        writer.startArray( "abilities" );
        for ( const fheroes2::MonsterAbility & ability : fheroes2::getMonsterData( unit.GetID() ).battleStats.abilities ) {
            const std::string description = fheroes2::getMonsterAbilityDescription( ability, true );

            if ( !description.empty() ) {
                writer.value( description );
            }
        }
        writer.endArray();

        writer.endObject();
    }

    void writeArmy( Json::Writer & writer, const char * key, Battle::Force & force, const Battle::Unit & currentUnit )
    {
        writer.startArray( key );

        for ( const Battle::Unit * unit : force ) {
            if ( unit == nullptr || !unit->isValid() ) {
                continue;
            }

            writeUnit( writer, *unit, unit->GetUID() == currentUnit.GetUID() );
        }

        writer.endArray();
    }

    void writeCommander( Json::Writer & writer, const char * key, const HeroBase * commander )
    {
        if ( commander == nullptr ) {
            return;
        }

        writer.startObject( key );
        writer.value( "name", commander->GetName() );
        writer.value( "attack", static_cast<int64_t>( commander->GetAttack() ) );
        writer.value( "defense", static_cast<int64_t>( commander->GetDefense() ) );
        writer.value( "power", static_cast<int64_t>( commander->GetPower() ) );
        writer.value( "knowledge", static_cast<int64_t>( commander->GetKnowledge() ) );
        writer.value( "spell_points", static_cast<int64_t>( commander->GetSpellPoints() ) );
        writer.value( "max_spell_points", static_cast<int64_t>( commander->GetMaxSpellPoints() ) );

        // Secondary skills change the very numbers a reader is asked to compute with - Archery
        // raises ranged damage, Armorer lowers damage taken - so they belong with the roster rather
        // than being left to be inferred from results. Only a full hero has them; a castle captain
        // does not, hence the check.
        writer.startArray( "skills" );
        if ( commander->isHeroes() ) {
            // The accessor is non-const, and nothing here modifies the hero.
            Heroes * hero = const_cast<Heroes *>( static_cast<const Heroes *>( commander ) );

            for ( const Skill::Secondary & skill : hero->GetSecondarySkills().ToVector() ) {
                if ( !skill.isValid() ) {
                    continue;
                }

                // Only the skills that change what happens in a battle. Navigation, Estates and
                // the rest matter on the adventure map and would be noise here - and noise in a
                // briefing is not free: it is re-read every time the model thinks.
                switch ( skill.Skill() ) {
                case Skill::Secondary::ARCHERY:
                case Skill::Secondary::BALLISTICS:
                case Skill::Secondary::LEADERSHIP:
                case Skill::Secondary::LUCK:
                    break;
                default:
                    continue;
                }

                writer.startObject();
                writer.value( "name", skill.GetName() );
                writer.value( "description", skill.GetDescription( *hero ) );
                writer.endObject();
            }
        }
        writer.endArray();

        writer.startArray( "spells" );
        if ( commander->HaveSpellBook() ) {
            for ( const Spell & spell : commander->getAllSpells() ) {
                writer.startObject();
                writer.value( "id", static_cast<int64_t>( spell.GetID() ) );
                writer.value( "name", spell.GetName() );
                writer.value( "cost", static_cast<int64_t>( spell.spellPoints( commander ) ) );

                // What the spell actually does, in the game's own words. A name and a price are not
                // enough to choose between spells, and a reader left to infer "Bless" or "Slow"
                // from the name alone will guess.
                //
                // getSpellDescription rather than Spell::GetDescription: the latter returns the
                // raw string, placeholders and all, so a description would arrive talking about
                // restoring "%{count} HP". This one substitutes them from the hero's spell power.
                writer.value( "description", fheroes2::getSpellDescription( spell, commander ) );
                writer.endObject();
            }
        }
        writer.endArray();

        writer.endObject();
    }

    // A spell this side's hero may cast right now, and what it may be aimed at.
    struct CastableSpell
    {
        int spellId{ 0 };
        // Unit uids this spell may target. Empty for spells that take no target.
        std::vector<uint32_t> targets;
        bool needsTarget{ false };
        bool needsDestination{ false };
    };

    // Works out which of the hero's spells can be cast this turn and at what. Everything here is
    // asked of the engine rather than reimplemented, so the list cannot drift from the real rules.
    void collectCastableSpells( Battle::Arena & arena, const Battle::Unit & currentUnit, std::vector<CastableSpell> & castable )
    {
        castable.clear();

        const HeroBase * commander = arena.GetCurrentCommander();
        if ( commander == nullptr || !commander->HaveSpellBook() ) {
            return;
        }

        // A hero casts at most one spell per turn, and some battles forbid magic entirely.
        if ( arena.isSpellcastDisabled() ) {
            return;
        }

        Battle::Force & friends = arena.getForce( currentUnit.GetCurrentColor() );
        Battle::Force & enemies = arena.getEnemyForce( currentUnit.GetCurrentColor() );

        for ( const Spell & spell : commander->getAllSpells() ) {
            if ( !spell.isCombat() || arena.isDisableCastSpell( spell ) || !commander->CanCastSpell( spell ) ) {
                continue;
            }

            CastableSpell entry;
            entry.spellId = spell.GetID();

            if ( spell.isApplyWithoutFocusObject() ) {
                // Armageddon and the like: cast at the battlefield, not at a unit.
                castable.push_back( std::move( entry ) );
                continue;
            }

            entry.needsTarget = true;
            // Teleport moves one of our own units to an empty cell, so it needs both.
            entry.needsDestination = ( spell.GetID() == Spell::TELEPORT );

            const bool onFriends = spell.isApplyToFriends() || spell.isApplyToAnyTroops();
            const bool onEnemies = spell.isApplyToEnemies() || spell.isApplyToAnyTroops();

            for ( const auto & [force, allowed] : { std::pair<Battle::Force *, bool>( &friends, onFriends ), std::pair<Battle::Force *, bool>( &enemies, onEnemies ) } ) {
                if ( !allowed ) {
                    continue;
                }

                for ( const Battle::Unit * unit : *force ) {
                    if ( unit == nullptr || !unit->isValid() ) {
                        continue;
                    }

                    // The engine decides whether the spell would actually do anything here; a spell
                    // with no effect on a unit is not a legal target for it.
                    if ( arena.GetTargetsForSpell( commander, spell, unit->GetHeadIndex() ).empty() ) {
                        continue;
                    }

                    entry.targets.push_back( unit->GetUID() );
                }
            }

            if ( !entry.targets.empty() ) {
                castable.push_back( std::move( entry ) );
            }
        }
    }

    void writeCastableSpells( Json::Writer & writer, const Battle::Arena & arena, const std::vector<CastableSpell> & castable )
    {
        const HeroBase * commander = arena.GetCurrentCommander();

        writer.startArray( "cast" );

        for ( const CastableSpell & entry : castable ) {
            const Spell spell( entry.spellId );

            writer.startObject();
            writer.value( "spell", static_cast<int64_t>( entry.spellId ) );
            writer.value( "name", spell.GetName() );
            writer.value( "cost", static_cast<int64_t>( spell.spellPoints( commander ) ) );
            writer.value( "needs_target", entry.needsTarget );
            writer.value( "needs_destination", entry.needsDestination );

            writer.startArray( "targets" );
            for ( const uint32_t uid : entry.targets ) {
                writer.value( static_cast<int64_t>( uid ) );
            }
            writer.endArray();

            writer.endObject();
        }

        writer.endArray();
    }

    // Enumerates what the current unit may legally do this turn, so that the harness picks from a
    // list instead of guessing at the rules.
    void writeLegalActions( Json::Writer & writer, Battle::Arena & arena, const Battle::Unit & currentUnit, std::vector<int32_t> & moveCells,
                            std::vector<uint32_t> & attackTargets, std::vector<CastableSpell> & castable )
    {
        moveCells.clear();
        attackTargets.clear();

        writer.startObject( "legal_actions" );

        writer.startArray( "move" );
        if ( !currentUnit.isImmovable() ) {
            for ( const int32_t cell : arena.getAllAvailableMoves( currentUnit ) ) {
                // getAllAvailableMoves() reports the head index of every reachable position, but
                // ApplyActionMove rejects a destination unless Position::GetReachable() puts the
                // HEAD on it - and for a two-cell unit that call can come back with the requested
                // cell as the tail instead. Such a cell is offered and then refused, and the stack
                // loses its whole turn to a move that never happens: observed costing a Cavalry
                // stack three rejected moves and its opening round.
                //
                // So the same test the engine will apply is applied here, and only cells that pass
                // it are offered.
                const Battle::Position position = Battle::Position::GetReachable( currentUnit, cell );

                if ( position.GetHead() == nullptr || position.GetHead()->GetIndex() != cell ) {
                    continue;
                }

                moveCells.push_back( cell );
                writer.value( static_cast<int64_t>( cell ) );
            }
        }
        writer.endArray();

        // Which of those cells leave the stack out of reach of every enemy. Working this out needs
        // the board's parity-dependent neighbours, and a player asked to derive them from a list of
        // cell names will do it by hand, one cell at a time, for as many cells as it was offered -
        // observed eating a whole turn's thinking on a 64-cell move list. The engine already knows,
        // so it says so.
        writer.startArray( "move_breaks_contact" );
        for ( const int32_t cell : moveCells ) {
            const Battle::Position position = Battle::Position::GetPosition( currentUnit, cell );

            if ( position.GetHead() == nullptr ) {
                continue;
            }

            bool adjacentToEnemy = false;

            for ( const int32_t around : Battle::Board::GetAroundIndexes( position ) ) {
                const Battle::Unit * occupant = Battle::Board::GetCell( around )->GetUnit();

                if ( occupant != nullptr && occupant->isValid() && occupant->GetCurrentColor() != currentUnit.GetCurrentColor() ) {
                    adjacentToEnemy = true;
                    break;
                }
            }

            if ( !adjacentToEnemy ) {
                writer.value( static_cast<int64_t>( cell ) );
            }
        }
        writer.endArray();

        writer.startArray( "attack" );
        Battle::Force & enemies = arena.getEnemyForce( currentUnit.GetCurrentColor() );

        for ( const Battle::Unit * enemy : enemies ) {
            if ( enemy == nullptr || !enemy->isValid() ) {
                continue;
            }

            // A ranged attacker with shots left and no adjacent enemy can hit anything on the board;
            // otherwise the target has to be reachable in melee.
            const bool canShoot = currentUnit.isArchers() && currentUnit.GetShots() > 0 && !currentUnit.isHandFighting();
            bool canReach = canShoot;

            if ( !canReach ) {
                for ( const int32_t around : Battle::Board::GetAroundIndexes( *enemy ) ) {
                    const Battle::Position position = Battle::Position::GetPosition( currentUnit, around );

                    if ( position.GetHead() != nullptr && arena.isPositionReachable( currentUnit, position, true ) ) {
                        canReach = true;
                        break;
                    }
                }
            }

            if ( !canReach ) {
                continue;
            }

            attackTargets.push_back( enemy->GetUID() );

            writer.startObject();
            writer.value( "target", static_cast<int64_t>( enemy->GetUID() ) );
            writer.value( "name", enemy->GetName() );
            writer.value( "ranged", canShoot );
            writer.endObject();
        }
        writer.endArray();

        collectCastableSpells( arena, currentUnit, castable );
        writeCastableSpells( writer, arena, castable );

        writer.value( "can_skip", true );
        writer.value( "can_retreat", arena.CanRetreatOpponent( currentUnit.GetCurrentColor() ) );
        writer.value( "can_surrender", arena.CanSurrenderOpponent( currentUnit.GetCurrentColor() ) );

        writer.endObject();
    }

    std::string buildTurnMessage( Battle::Arena & arena, const Battle::Unit & currentUnit, const int side, std::vector<int32_t> & moveCells,
                                  std::vector<uint32_t> & attackTargets, std::vector<CastableSpell> & castable )
    {
        Json::Writer writer;

        writer.startObject();
        writer.value( "type", "turn" );
        writer.value( "protocol", static_cast<int64_t>( protocolVersion ) );
        writer.value( "turn", static_cast<int64_t>( arena.GetTurnNumber() ) );
        writer.value( "side", sideName( side ) );
        writer.value( "color", colorName( currentUnit.GetCurrentColor() ) );

        writer.startObject( "unit" );
        writer.value( "uid", static_cast<int64_t>( currentUnit.GetUID() ) );
        writer.value( "name", currentUnit.GetName() );
        writer.value( "count", static_cast<int64_t>( currentUnit.GetCount() ) );
        writer.value( "head", static_cast<int64_t>( currentUnit.GetHeadIndex() ) );
        writer.value( "speed", static_cast<int64_t>( currentUnit.GetSpeed() ) );
        writer.value( "shots", static_cast<int64_t>( currentUnit.GetShots() ) );
        writer.endObject();

        writeArmy( writer, "attacker_army", arena.getAttackingForce(), currentUnit );
        writeArmy( writer, "defender_army", arena.getDefendingForce(), currentUnit );

        writeCommander( writer, "attacker_commander", arena.getAttackingForce().GetCommander() );
        writeCommander( writer, "defender_commander", arena.getDefendingForce().GetCommander() );

        writeLegalActions( writer, arena, currentUnit, moveCells, attackTargets, castable );

        writer.endObject();

        return writer.str();
    }

    std::string buildErrorMessage( const std::string & reason )
    {
        Json::Writer writer;

        writer.startObject();
        writer.value( "type", "error" );
        writer.value( "protocol", static_cast<int64_t>( protocolVersion ) );
        writer.value( "message", reason );
        writer.endObject();

        return writer.str();
    }

    Connection & connectionFor( const int side )
    {
        const auto existing = connections.find( side );
        if ( existing != connections.end() && existing->second->isOpen() ) {
            return *existing->second;
        }

        const std::string & endpoint = sideConfigs[static_cast<size_t>( side )].endpoint;

        std::string host;
        std::string port;
        std::string error;

        if ( !parseTcpEndpoint( endpoint, host, port, error ) ) {
            throw ExternalAI::ConnectionError( "the endpoint for the " + std::string( sideName( side ) ) + " is unusable: " + error );
        }

        auto connection = std::make_unique<Connection>( endpoint );
        connection->connect( host, port );

        Json::Writer hello;
        hello.startObject();
        hello.value( "type", "hello" );
        hello.value( "protocol", static_cast<int64_t>( protocolVersion ) );
        hello.value( "game", "fheroes2-battle" );
        hello.value( "side", sideName( side ) );
        hello.endObject();

        // The harness must acknowledge before the battle starts, so that a misconfigured or
        // incompatible harness is caught here rather than halfway through a battle.
        const std::string reply = awaitReply( *connection, hello.str(), _( "Waiting for the external AI..." ) );

        Json::Value parsed;
        std::string parseError;

        if ( !Json::parse( reply, parsed, parseError ) ) {
            throw ExternalAI::ConnectionError( "the harness answered the handshake with something that is not JSON (" + parseError + ")" );
        }

        const Json::Value * type = parsed.find( "type" );
        if ( type == nullptr || type->asString() != "ready" ) {
            throw ExternalAI::ConnectionError( "the harness did not answer the handshake with {\"type\": \"ready\"}" );
        }

        connections[side] = std::move( connection );

        return *connections[side];
    }

    // Translates one harness reply into commands. Returns an empty string on success, or the reason
    // the action was rejected, which is sent back so the harness can correct itself.
    std::string applyAction( const Json::Value & reply, Battle::Arena & arena, const Battle::Unit & currentUnit, Battle::Actions & actions,
                             const std::vector<int32_t> & moveCells, const std::vector<uint32_t> & attackTargets, const std::vector<CastableSpell> & castable )
    {
        const Json::Value * actionField = reply.find( "action" );
        if ( actionField == nullptr || !actionField->isString() ) {
            return "the reply has no \"action\" field holding a string";
        }

        const std::string & action = actionField->asString();

        if ( action == "skip" ) {
            actions.emplace_back( Battle::Command::SKIP, currentUnit.GetUID() );
            return {};
        }

        if ( action == "retreat" ) {
            if ( !arena.CanRetreatOpponent( currentUnit.GetCurrentColor() ) ) {
                return "this side cannot retreat";
            }

            actions.emplace_back( Battle::Command::RETREAT );
            return {};
        }

        if ( action == "surrender" ) {
            if ( !arena.CanSurrenderOpponent( currentUnit.GetCurrentColor() ) ) {
                return "this side cannot surrender";
            }

            actions.emplace_back( Battle::Command::SURRENDER );
            return {};
        }

        if ( action == "cast" ) {
            const Json::Value * spellField = reply.find( "spell" );
            if ( spellField == nullptr || !spellField->isNumber() ) {
                return "\"cast\" needs a numeric \"spell\" field holding a spell id from legal_actions.cast";
            }

            const int spellId = static_cast<int>( spellField->asInt() );

            const auto entry = std::find_if( castable.begin(), castable.end(), [spellId]( const CastableSpell & candidate ) { return candidate.spellId == spellId; } );
            if ( entry == castable.end() ) {
                return "spell " + std::to_string( spellId ) + " is not in this turn's legal_actions.cast";
            }

            if ( !entry->needsTarget ) {
                // Cast at the battlefield rather than at anything in particular.
                actions.emplace_back( Battle::Command::SPELLCAST, spellId, -1 );
                return {};
            }

            const Json::Value * targetField = reply.find( "target" );
            if ( targetField == nullptr || !targetField->isNumber() ) {
                return "casting " + std::string( Spell( spellId ).GetName() ) + " needs a numeric \"target\" field holding a unit uid";
            }

            const uint32_t target = static_cast<uint32_t>( targetField->asInt() );

            if ( std::find( entry->targets.begin(), entry->targets.end(), target ) == entry->targets.end() ) {
                return "unit " + std::to_string( target ) + " is not a legal target for " + Spell( spellId ).GetName();
            }

            const Battle::Unit * victim = arena.GetTroopUID( target );
            if ( victim == nullptr || !victim->isValid() ) {
                return "unit " + std::to_string( target ) + " is no longer on the battlefield";
            }

            if ( !entry->needsDestination ) {
                actions.emplace_back( Battle::Command::SPELLCAST, spellId, victim->GetHeadIndex() );
                return {};
            }

            // Teleport is the one spell that moves a unit, so it needs somewhere to move it to.
            const Json::Value * cellField = reply.find( "cell" );
            if ( cellField == nullptr || !cellField->isNumber() ) {
                return "casting Teleport needs a numeric \"cell\" field holding the destination";
            }

            const int32_t destination = static_cast<int32_t>( cellField->asInt() );

            const Battle::Position position = Battle::Position::GetPosition( *victim, destination );
            if ( position.GetHead() == nullptr ) {
                return "cell " + std::to_string( destination ) + " cannot hold that unit";
            }

            actions.emplace_back( Battle::Command::SPELLCAST, spellId, victim->GetHeadIndex(), destination );
            return {};
        }

        if ( action == "move" ) {
            const Json::Value * cellField = reply.find( "cell" );
            if ( cellField == nullptr || !cellField->isNumber() ) {
                return "\"move\" needs a numeric \"cell\" field";
            }

            const int32_t cell = static_cast<int32_t>( cellField->asInt() );

            if ( std::find( moveCells.begin(), moveCells.end(), cell ) == moveCells.end() ) {
                return "cell " + std::to_string( cell ) + " is not in this turn's legal_actions.move";
            }

            actions.emplace_back( Battle::Command::MOVE, currentUnit.GetUID(), cell );
            return {};
        }

        if ( action == "attack" ) {
            const Json::Value * targetField = reply.find( "target" );
            if ( targetField == nullptr || !targetField->isNumber() ) {
                return "\"attack\" needs a numeric \"target\" field holding an enemy uid";
            }

            const uint32_t target = static_cast<uint32_t>( targetField->asInt() );

            if ( std::find( attackTargets.begin(), attackTargets.end(), target ) == attackTargets.end() ) {
                return "unit " + std::to_string( target ) + " is not in this turn's legal_actions.attack";
            }

            const Battle::Unit * defender = arena.GetTroopUID( target );
            if ( defender == nullptr || !defender->isValid() ) {
                return "unit " + std::to_string( target ) + " is no longer on the battlefield";
            }

            // A ranged attack needs no approach; a melee one is resolved by the engine from the
            // attacker's current position when no explicit cell is given.
            const bool isRanged = currentUnit.isArchers() && currentUnit.GetShots() > 0 && !currentUnit.isHandFighting();

            if ( isRanged ) {
                // A shot is taken from where the unit stands, and carries no attack direction:
                // CellDirection::UNKNOWN is 0, and the engine rejects a shot that claims one.
                actions.emplace_back( Battle::Command::ATTACK, currentUnit.GetUID(), defender->GetUID(), -1, -1, 0 );
                return {};
            }

            // Approach the target if we are not already next to it, picking a reachable neighbouring
            // cell. Which one is left to the engine's pathfinder rather than to the harness.
            int32_t moveTarget = -1;

            if ( !Battle::Unit::isHandFighting( currentUnit, *defender ) ) {
                for ( const int32_t around : Battle::Board::GetAroundIndexes( *defender ) ) {
                    const Battle::Position position = Battle::Position::GetPosition( currentUnit, around );

                    if ( position.GetHead() != nullptr && arena.isPositionReachable( currentUnit, position, true ) ) {
                        moveTarget = position.GetHead()->GetIndex();
                        break;
                    }
                }

                if ( moveTarget < 0 ) {
                    return "unit " + std::to_string( target ) + " cannot be reached this turn";
                }
            }

            // -1 for both the attacked cell and the direction asks the engine to work them out from
            // the attacker's position. A melee attack needs a real direction, and passing 0 here
            // means CellDirection::UNKNOWN, which the engine rejects - silently, which cost an
            // afternoon: the command is dropped, the unit never moves, and the turn never ends.
            actions.emplace_back( Battle::Command::ATTACK, currentUnit.GetUID(), defender->GetUID(), moveTarget, -1, -1 );
            return {};
        }

        return "unknown action '" + action + "'; expected one of move, attack, cast, skip, retreat, surrender";
    }
}

bool ExternalAI::parseModeSpec( const std::string & spec, ControlMode & mode, std::string & endpoint, std::string & error )
{
    endpoint.clear();

    if ( spec == "human" ) {
        mode = ControlMode::HUMAN;
        return true;
    }

    if ( spec == "ai" ) {
        mode = ControlMode::GAME_AI;
        return true;
    }

    constexpr const char * externalPrefix = "external:";
    constexpr size_t externalPrefixLength = 9;

    if ( spec.compare( 0, externalPrefixLength, externalPrefix ) == 0 ) {
        endpoint = spec.substr( externalPrefixLength );

        std::string host;
        std::string port;

        if ( !parseTcpEndpoint( endpoint, host, port, error ) ) {
            return false;
        }

        mode = ControlMode::EXTERNAL;
        return true;
    }

    error = "expected 'human', 'ai', or 'external:tcp://<host>:<port>'";
    return false;
}

void ExternalAI::setReplyTimeout( const int seconds )
{
    if ( seconds > 0 ) {
        replyTimeoutSeconds = seconds;
    }
}

void ExternalAI::setSideMode( const int side, const ControlMode mode, const std::string & endpoint )
{
    if ( !isValidSide( side ) ) {
        return;
    }

    sideConfigs[static_cast<size_t>( side )].mode = mode;

    if ( !endpoint.empty() ) {
        sideConfigs[static_cast<size_t>( side )].endpoint = endpoint;
    }
}

ExternalAI::ControlMode ExternalAI::sideMode( const int side )
{
    if ( !isValidSide( side ) ) {
        return ControlMode::HUMAN;
    }

    return sideConfigs[static_cast<size_t>( side )].mode;
}

bool ExternalAI::hasEndpoint( const int side )
{
    return isValidSide( side ) && !sideConfigs[static_cast<size_t>( side )].endpoint.empty();
}

void ExternalAI::cycleSideMode( const int side )
{
    if ( !isValidSide( side ) ) {
        return;
    }

    switch ( sideMode( side ) ) {
    case ControlMode::HUMAN:
        setSideMode( side, ControlMode::GAME_AI, {} );
        break;
    case ControlMode::GAME_AI:
        // Only offer the external mode when there is somewhere to connect to.
        setSideMode( side, hasEndpoint( side ) ? ControlMode::EXTERNAL : ControlMode::HUMAN, {} );
        break;
    case ControlMode::EXTERNAL:
        setSideMode( side, ControlMode::HUMAN, {} );
        break;
    }
}

const char * ExternalAI::sideModeName( const int side )
{
    switch ( sideMode( side ) ) {
    case ControlMode::HUMAN:
        return "Human";
    case ControlMode::GAME_AI:
        return "Game AI";
    case ControlMode::EXTERNAL:
        return "External AI";
    }

    return "Human";
}

void ExternalAI::bindColorToSide( const PlayerColor color, const int side )
{
    if ( isValidSide( side ) ) {
        colorToSide[color] = side;
    }
}

const char * ExternalAI::controllerName( const PlayerColor color )
{
    const auto side = colorToSide.find( color );

    return ( side == colorToSide.end() ) ? "Game AI" : sideModeName( side->second );
}

bool ExternalAI::isExternalColor( const PlayerColor color )
{
    const auto side = colorToSide.find( color );
    if ( side == colorToSide.end() ) {
        return false;
    }

    return sideMode( side->second ) == ControlMode::EXTERNAL;
}

void ExternalAI::takeTurn( Battle::Arena & arena, const Battle::Unit & currentUnit, Battle::Actions & actions )
{
    const auto sideEntry = colorToSide.find( currentUnit.GetCurrentColor() );
    if ( sideEntry == colorToSide.end() ) {
        throw ConnectionError( "no side is bound to the colour currently taking its turn" );
    }

    const int side = sideEntry->second;

    Connection & connection = connectionFor( side );

    std::vector<int32_t> moveCells;
    std::vector<uint32_t> attackTargets;
    std::vector<CastableSpell> castable;

    std::string request = buildTurnMessage( arena, currentUnit, side, moveCells, attackTargets, castable );

    for ( int attempt = 0;; ++attempt ) {
        std::string waitingFor = _( "The external AI is thinking" );
        if ( attempt > 0 ) {
            waitingFor = _( "The external AI is reconsidering" );
        }

        const std::string reply = awaitReply( connection, request, waitingFor );

        Json::Value parsed;
        std::string rejection;

        if ( !Json::parse( reply, parsed, rejection ) ) {
            rejection = "the reply is not valid JSON (" + rejection + ")";
        }
        else {
            rejection = applyAction( parsed, arena, currentUnit, actions, moveCells, attackTargets, castable );

            if ( rejection.empty() ) {
                return;
            }
        }

        if ( attempt >= maxIllegalActionRetries ) {
            ERROR_LOG( "External AI for the " << sideName( side ) << " gave " << ( maxIllegalActionRetries + 1 ) << " unusable actions in a row (" << rejection
                                              << "); skipping the turn of " << currentUnit.GetName() )

            // Skipping is deliberate: it is deterministic, shows up in the battle log, and cannot be
            // mistaken for the harness having played well.
            actions.emplace_back( Battle::Command::SKIP, currentUnit.GetUID() );
            return;
        }

        request = buildErrorMessage( rejection );
    }
}

void ExternalAI::endBattle()
{
    connections.clear();
}
